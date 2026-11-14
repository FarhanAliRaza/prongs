"""What a change to a Python file means, unit by unit.

The selector used to read a diff as lines: a changed line inside a function
changed that function, and any other changed line was "module-level", which
takes the file wholesale and, for a file imported at collection time, runs
everything. Lines are the wrong unit. Two ideas from elsewhere fix that:

  early cutoff (Salsa)   a changed input invalidates what depends on it only
                         if the value derived from it changed. Here the input
                         is a file's text and the derived values are
                         fingerprints of its units: a function body, and each
                         statement of the module and of its classes. A diff
                         that leaves every fingerprint equal (formatting,
                         comments, `import a` -> `import a as a`) is no
                         change at all.
  code blocks (testmon)  a fingerprint is the AST dump of the unit, without
                         positions, with the body of every nested function
                         cut out: a function's body is its own unit, so
                         editing it changes neither its parent nor the module
                         statement that defines it.

On top of the fingerprints, a module-level change is read as a change to
*bindings*: which names the module binds, and to what. A name the old module
never bound and never looked up has nothing that can observe it, so adding
its binding (a new function, a new constant, a new import) changes no
existing test. The code that uses the new name is itself a change, and is
selected as one. Removing an import nothing refers to is the same argument
backwards. Anything else at module or class level (a changed assignment, a
class statement, a decorator's module, a statement with a side effect) is
still a module-level change, reported with what it was.

Everything here is static and conservative: when a rule cannot be shown to
hold, the change is reported as module-level and the selector does what it
always did. What the rules cannot see is listed in the README.
"""

from __future__ import annotations

import ast
import symtable
from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Protocol

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)
# decorators that wrap the function without running project code at import
PLAIN_DECORATORS = {"staticmethod", "classmethod", "property"}
# builtins whose call builds a value and does nothing else
PURE_BUILTINS = {
    "bool", "bytes", "dict", "float", "frozenset", "int", "len", "list", "max", "min",
    "object", "range", "set", "sorted", "str", "sum", "tuple",
}


def _docstring(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[0], ast.Expr) and isinstance(
        body[0].value, ast.Constant) and isinstance(body[0].value.value, str)


def _body(node, ignore_docstrings: bool) -> list[ast.stmt]:
    return node.body[1:] if ignore_docstrings and _docstring(node.body) else node.body


def dump(node, ignore_docstrings: bool = False) -> str:
    """`ast.dump` without positions, and without the body of any function:
    a body is a unit of its own (dump its statement list to get it)."""
    if isinstance(node, list):
        return "[" + ", ".join(dump(n, ignore_docstrings) for n in node) + "]"
    if not isinstance(node, ast.AST):
        return repr(node)
    parts = []
    for name, value in ast.iter_fields(node):
        if name == "body" and isinstance(node, FUNC):
            continue
        elif name == "body" and isinstance(node, (ast.ClassDef, ast.Module)):
            value = _body(node, ignore_docstrings)
        elif name == "asname" and value is None and isinstance(node, ast.alias):
            # `import a` and `import a as a`, `from m import a` and `from m
            # import a as a` bind the same name to the same object
            value = node.name if "." not in node.name else None
        parts.append(f"{name}={dump(value, ignore_docstrings)}")
    return f"{type(node).__name__}({', '.join(parts)})"


@dataclass
class Item:
    """One statement of a module or class body, as a binding."""
    kind: str    # def | class | assign | import | stmt
    name: str    # the name it binds; '' for a statement that is not one binding
    fp: str
    node: ast.AST = field(compare=False, repr=False)
    alias: ast.alias | None = field(default=None, compare=False, repr=False)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.kind, self.name, self.fp

    @property
    def line(self) -> int:
        return getattr(self.node, "lineno", 0)


def _items(body: list[ast.stmt], ignore_docstrings: bool) -> list[Item]:
    out: list[Item] = []
    for i, st in enumerate(body):
        if i == 0 and ignore_docstrings and _docstring(body):
            continue
        fp = dump(st, ignore_docstrings)
        if isinstance(st, FUNC):
            out.append(Item("def", st.name, fp, st))
        elif isinstance(st, ast.ClassDef):
            # the header only: the body is a scope of its own
            header = ast.ClassDef(
                name=st.name, bases=st.bases, keywords=st.keywords, body=[],
                decorator_list=st.decorator_list, type_params=getattr(st, "type_params", []),
            )
            out.append(Item("class", st.name, dump(header), st))
        elif isinstance(st, (ast.Import, ast.ImportFrom)):
            mod = "" if isinstance(st, ast.Import) else f"{'.' * st.level}{st.module or ''}:"
            for a in st.names:
                if a.name == "*":
                    out.append(Item("stmt", "", fp, st))
                    continue
                bound = a.asname or a.name.split(".")[0]
                # `import a.b` binds `a` and loads `a.b`: the target is part of the binding
                out.append(Item("import", bound, f"{mod}{a.name}", st, a))
        elif (isinstance(st, ast.Assign) and len(st.targets) == 1
              and isinstance(st.targets[0], ast.Name)):
            out.append(Item("assign", st.targets[0].id, fp, st))
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            out.append(Item("assign", st.target.id, fp, st))
        else:
            out.append(Item("stmt", "", fp, st))
    return out


class Module:
    """A parsed module: function bodies by qualname, and the statements of
    the module and of each class directly reachable from it."""

    def __init__(self, source: str, ignore_docstrings: bool = False):
        self.tree = ast.parse(source)
        self.ignore_docstrings = ignore_docstrings
        self.funcs: dict[str, list[tuple[str, str]]] = {}  # qualname -> (header, body) fingerprints
        self.scopes: dict[str, list[Item]] = {}   # '' or class qualname -> statements
        self.scope_defs: set[str] = set()         # functions defined directly in a scope
        self.strings: set[str] = set()
        self.star_import = False
        self._walk(self.tree, "")
        self._scope(self.tree.body, "")
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.strings.add(node.value)
            elif isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
                self.star_import = True
        # names as the compiler resolves them: what the module binds, and
        # every lookup that goes to the module's globals (or past them, to a
        # builtin). A function's own locals are neither.
        self.bound: set[str] = set()
        self.loads: set[str] = set()
        top = symtable.symtable(source, "<module>", "exec")
        for sym in top.get_symbols():
            if sym.is_assigned() or sym.is_imported() or sym.is_namespace():
                self.bound.add(sym.get_name())
            if sym.is_referenced():
                self.loads.add(sym.get_name())
        stack = list(top.get_children())
        while stack:
            table = stack.pop()
            stack += table.get_children()
            for sym in table.get_symbols():
                if sym.is_global():
                    if sym.is_referenced():
                        self.loads.add(sym.get_name())
                    if sym.is_assigned():  # `global x; x = ...` inside a function
                        self.bound.add(sym.get_name())

    def _walk(self, node, prefix: str) -> None:
        # the qualnames the recorder sees: Class.method, outer.<locals>.inner
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNC):
                qual = f"{prefix}{child.name}"
                self.funcs.setdefault(qual, []).append((
                    dump(child, self.ignore_docstrings),
                    dump(_body(child, self.ignore_docstrings), self.ignore_docstrings)))
                self._walk(child, f"{qual}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                self._walk(child, f"{prefix}{child.name}.")
            else:
                self._walk(child, prefix)

    def _scope(self, body: list[ast.stmt], qual: str) -> None:
        items = self.scopes[qual] = _items(body, self.ignore_docstrings)
        prefix = f"{qual}." if qual else ""
        for it in items:
            if it.kind == "def":
                self.scope_defs.add(prefix + it.name)
            elif it.kind == "class" and prefix + it.name not in self.scopes:
                self._scope(it.node.body, prefix + it.name)

    def binds(self, name: str) -> bool:
        """Whether the module's code binds `name` as a global."""
        return name in self.bound

    def has_all(self) -> bool:
        return any(it.kind == "assign" and it.name == "__all__" for it in self.scopes[""])

    def import_targets(self) -> dict[str, ast.stmt]:
        """name -> the import statement that binds it at module level."""
        return {it.name: it.node for it in self.scopes[""] if it.kind == "import"}


class Project(Protocol):
    """What the rules need to know about the rest of the repository."""

    def module_state(self, path: str, node: ast.stmt, alias: ast.alias) -> str:
        """What one imported name loads: 'external' (no project module),
        'loaded' (project modules that collection already imports) or
        'unloaded' (a project module collection did not import)."""

    def still_imported(self, path: str, node: ast.stmt, alias: ast.alias) -> bool:
        """Whether every project module one imported name loads is also
        imported by another file that collection imports."""

    def provides(self, path: str, node: ast.stmt, alias: ast.alias) -> bool:
        """Whether a `from module import name` can succeed: always for a
        module outside the project, which cannot be read."""

    def star_imported(self, path: str, name: str) -> bool:
        """Whether a file that does `from <path's module> import *` could
        observe `name` arriving in, or leaving, its own namespace."""

    def used_elsewhere(self, path: str, name: str) -> bool:
        """Whether another file imports `name` from this module or reads it
        as an attribute of it."""


@dataclass
class Delta:
    """What changed between two versions of one file."""
    changed: set[str] = field(default_factory=set)   # functions that existed and differ, or are gone
    headers: set[str] = field(default_factory=set)   # of those: the def statement itself differs
    added: set[str] = field(default_factory=set)     # new functions something may already reach
    module_level: list[str] = field(default_factory=list)  # why the file is taken wholesale
    additive: list[str] = field(default_factory=list)      # bindings added or removed unobserved
    scopes: set[str] = field(default_factory=set)  # '' or a class: its set of names changed


def pure(node, mod: Module, path: str, project: Project) -> bool:
    """Whether evaluating an expression can only build a value: constants,
    names, containers, operators, and calls into builtins or into modules
    that are not this project's."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(pure(e, mod, path, project) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(pure(e, mod, path, project) for e in (*node.keys, *node.values) if e)
    if isinstance(node, ast.Attribute):
        return pure(node.value, mod, path, project)
    if isinstance(node, ast.UnaryOp):
        return pure(node.operand, mod, path, project)
    if isinstance(node, ast.BinOp):
        return pure(node.left, mod, path, project) and pure(node.right, mod, path, project)
    if isinstance(node, ast.JoinedStr):
        return all(pure(v, mod, path, project) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return pure(node.value, mod, path, project)
    if isinstance(node, ast.Call):
        root = node.func
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return False
        imports = mod.import_targets()
        if root.id in imports:
            st = imports[root.id]
            alias = next(a for a in st.names if (a.asname or a.name.split(".")[0]) == root.id)
            if project.module_state(path, st, alias) != "external":
                return False
        elif not (root.id in PURE_BUILTINS and node.func is root and not mod.binds(root.id)):
            return False
        return all(pure(a, mod, path, project) for a in node.args) and all(
            pure(k.value, mod, path, project) for k in node.keywords)
    return False


def _plain_def(node, mod: Module, path: str, project: Project) -> bool:
    """A def statement that only binds its name: no decorator that runs
    project code, no default that does."""
    for d in node.decorator_list:
        if not (isinstance(d, ast.Name) and d.id in PLAIN_DECORATORS and not mod.binds(d.id)):
            return False
    a = node.args
    return all(pure(d, mod, path, project) for d in (*a.defaults, *a.kw_defaults) if d)


def _fresh(name: str, old: Module, new: Module, path: str, project: Project) -> str | None:
    """Why a module-level name the new version binds could already be
    observed, or None when nothing can observe it."""
    if name.startswith("__") and name.endswith("__"):
        return "a dunder name is looked up by the interpreter"
    if old.binds(name):
        return "the name was already bound"
    if name in old.loads:
        return "the old module looked the name up (a builtin or a star import)"
    if name in old.strings:
        return "the old module names it in a string"
    if old.star_import or new.star_import:
        return "the module star-imports names"
    if old.binds("__getattr__"):
        return "the module defines __getattr__"
    if not name.startswith("_") and not old.has_all() and project.star_imported(path, name):
        return "the module is star-imported"
    return None


def _removable(name: str, new: Module, path: str, project: Project) -> str | None:
    """Why a module-level name the new version no longer binds could still
    be wanted, or None."""
    if name in new.loads or name in new.bound:
        return "the module still refers to it"
    if name in new.strings:
        return "the module names it in a string"
    if new.star_import:
        return "the module star-imports names"
    if not name.startswith("_") and not new.has_all() and project.star_imported(path, name):
        return "the module is star-imported"
    if project.used_elsewhere(path, name):
        return "another file takes it from this module"
    return None


def _describe(it: Item) -> str:
    what = {"def": f"def {it.name}", "class": f"class {it.name}",
            "assign": f"assignment to {it.name}", "import": f"import of {it.name}",
            "stmt": type(it.node).__name__ + " statement"}[it.kind]
    return f"{what} (line {it.line})"


def _diff_scope(qual: str, old: Module, new: Module, path: str, project: Project,
                delta: Delta) -> None:
    a_items, b_items = old.scopes[qual], new.scopes[qual]
    prefix = f"{qual}." if qual else ""
    where = f" in class {qual}" if qual else ""
    # a test that lists the scope's names sees one appear or disappear
    if {i.name for i in a_items} != {i.name for i in b_items}:
        delta.scopes.add(qual)
    a_count, b_count = Counter(i.key for i in a_items), Counter(i.key for i in b_items)
    removed = [i for i in a_items if i.key not in b_count or a_count[i.key] > b_count[i.key]]
    added = [i for i in b_items if i.key not in a_count or b_count[i.key] > a_count[i.key]]
    if not removed and not added:
        # the same statements: only their order can differ. Definitions bind
        # without running anything, so their order cannot matter; imports
        # bind distinct names, so theirs can only matter by failing (a cycle,
        # a name used before it is imported), which collecting the suite
        # shows. Any other reordering is a module-level change.
        def order(items: list[Item], *skip: str) -> list[tuple]:
            return [i.key for i in items if i.kind not in skip]

        imports = [i.name for i in b_items if i.kind == "import"]
        if order(a_items, "def") == order(b_items, "def"):
            return
        if (order(a_items, "def", "import") == order(b_items, "def", "import")
                and len(imports) == len(set(imports)) and not qual):
            delta.additive.append("imports reordered")
        else:
            delta.module_level.append(f"statements reordered{where}")
        return
    names = {(i.kind, i.name) for i in removed + added}
    for kind, name in sorted(names):
        gone = [i for i in removed if (i.kind, i.name) == (kind, name)]
        came = [i for i in added if (i.kind, i.name) == (kind, name)]
        if kind == "def":
            # a changed header (signature, decorators) or a removed function:
            # the function itself changed, as it always did
            if gone:
                delta.changed.add(prefix + name)
                continue
            it = came[0]
            if qual or len(came) > 1:
                why = "a method can override an inherited one or be found by name"
            elif not _plain_def(it.node, new, path, project):
                why = "its decorator or a default runs code at import"
            else:
                why = _fresh(name, old, new, path, project)
            if why is None:
                delta.additive.append(f"new {_describe(it)}")
            else:
                delta.added.add(prefix + name)
            continue
        if qual:
            delta.module_level += [f"{_describe(i)}{where} "
                                   f"{'removed' if i in gone else 'added or changed'}"
                                   for i in gone + came]
            continue
        if kind == "import" and came and not gone and len(came) == 1:
            it = came[0]
            why = _fresh(name, old, new, path, project)
            if why is None and project.module_state(path, it.node, it.alias) == "unloaded":
                why = "it imports a project module nothing imported before"
            if why is None and not project.provides(path, it.node, it.alias):
                why = "the module it names does not define it"
            if why is None:
                delta.additive.append(f"new {_describe(it)}")
            else:
                delta.module_level.append(f"new {_describe(it)}: {why}")
        elif kind == "import" and gone and not came and len(gone) == 1:
            it = gone[0]
            why = _removable(name, new, path, project)
            if why is None and not project.still_imported(path, it.node, it.alias):
                why = "nothing else imports that project module"
            if why is None:
                delta.additive.append(f"removed {_describe(it)}")
            else:
                delta.module_level.append(f"removed {_describe(it)}: {why}")
        elif kind == "assign" and came and not gone and len(came) == 1:
            it = came[0]
            why = _fresh(name, old, new, path, project)
            if why is None and (it.node.value is None
                                or not pure(it.node.value, new, path, project)):
                why = "its value is not a plain expression"
            if why is None:
                delta.additive.append(f"new {_describe(it)}")
            else:
                delta.module_level.append(f"new {_describe(it)}: {why}")
        else:
            delta.module_level += [f"{_describe(i)} removed" for i in gone]
            delta.module_level += [f"{_describe(i)} added or changed" for i in came]


def delta(old_source: str, new_source: str, path: str, project: Project,
          ignore_docstrings: bool = False) -> Delta | None:
    """What changed from `old_source` to `new_source`, or None when either
    does not parse (the caller falls back to lines)."""
    try:
        old = Module(old_source, ignore_docstrings)
        new = Module(new_source, ignore_docstrings)
    except (SyntaxError, ValueError, RecursionError):
        return None
    d = Delta()
    for qual in old.funcs.keys() | new.funcs.keys():
        if qual not in new.funcs:
            d.changed.add(qual)
            d.headers.add(qual)
        elif qual in old.funcs and old.funcs[qual] != new.funcs[qual]:
            d.changed.add(qual)
            if [h for h, _ in old.funcs[qual]] != [h for h, _ in new.funcs[qual]]:
                d.headers.add(qual)
        # a new function is decided where it is defined: by its scope when
        # that is the module or a class, by its changed parent otherwise
    for qual in old.scopes.keys() | new.scopes.keys():
        if qual in old.scopes and qual in new.scopes:
            before = len(d.module_level)
            _diff_scope(qual, old, new, path, project, d)
            if len(d.module_level) > before:
                d.scopes.add(qual)  # a rebound name can change what a listing shows
    d.added -= d.changed
    return d


def module_candidates(dotted: str) -> list[str]:
    """The paths a dotted module name can live at, for it and every package
    above it: importing a.b.c imports a and a.b as well."""
    parts = [p for p in dotted.split(".") if p]
    out = []
    for i in range(1, len(parts) + 1):
        rel = "/".join(parts[:i])
        out += [f"{rel}.py", f"{rel}/__init__.py"]
    return out


def import_candidates(path: str, node: ast.stmt, alias: ast.alias | None = None) -> list[str]:
    """The paths the modules one import statement loads can have. An
    absolute import may sit under any source root, so its candidates are
    path suffixes; a relative import resolves against `path`, and its
    candidates are whole paths, marked with a leading '='."""
    names = [alias] if alias else node.names
    if isinstance(node, ast.Import):
        return [c for a in names for c in module_candidates(a.name)]
    module = node.module or ""
    dotted = [module] + [f"{module}.{a.name}".strip(".") for a in names if a.name != "*"]
    cands = [c for d in dotted for c in module_candidates(d)]
    if not node.level:
        return cands
    base = PurePosixPath(path).parent
    for _ in range(node.level - 1):
        base = base.parent
    root = "" if str(base) == "." else f"{base}/"
    return [f"={root}{c}" for c in [*cands, "__init__.py"]]


def from_candidates(path: str, node: ast.ImportFrom) -> list[str]:
    """The paths the module a `from ... import` names can have: that module
    only, not the packages above it or the names taken from it."""
    parts = [p for p in (node.module or "").split(".") if p]
    rel = "/".join(parts)
    cands = [f"{rel}.py", f"{rel}/__init__.py"] if rel else ["__init__.py"]
    if not node.level:
        return cands if rel else []
    base = PurePosixPath(path).parent
    for _ in range(node.level - 1):
        base = base.parent
    root = "" if str(base) == "." else f"{base}/"
    return [f"={root}{c}" for c in cands]


def matches(candidate: str, path: str) -> bool:
    """Whether a candidate from `import_candidates` names `path`: exactly for
    a relative import (a leading '='), as a path suffix for an absolute one."""
    if candidate.startswith("="):
        return candidate[1:] == path
    return path == candidate or path.endswith("/" + candidate)
