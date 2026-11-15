"""What import leaves behind, fingerprinted.

A change to code that runs at import (a module-level statement, a class
attribute, a function a module body calls) used to run everything, because
function entry cannot say which tests use the objects that code built. This
module asks the other question, Salsa's: did what import *produced* change,
and who can reach it?

  snapshot()   after collection, the graph of objects reachable from every
               global of every project module and every attribute of every
               project class. A class is a boundary: whatever holds one
               holds a reference, and the class's own attributes are roots
               of their own. Each root gets a fingerprint (cycle-safe: a
               strongly connected component is fingerprinted as a whole) and
               the set of project classes its graph refers to. Functions are
               fingerprinted without their code: a changed body is found by
               the tests that enter it.
  volatile()   what differs between two snapshots of one tree (a lock, a
               timestamp, a cache keyed by a salted hash), down to the
               attribute or key of a root: never a sign of a change.
  diff()       between a recorded snapshot and one of the tree as it is now:
               the globals whose fingerprint changed, the classes whose
               definition changed (and their subclasses), and every global
               or class attribute that holds a changed class or an instance
               of one.
  reach()      the tests that can get at any of that: those that entered a
               function whose source names a changed global, a changed class
               or something holding one, or a method of a changed class.

With `import_state = true` in [tool.prongs], a recorded run stores a
snapshot, and a change that would run everything only because import-time
code changed selects by this instead: the tests the other rules select, plus
those that reach what import now produces differently. `"shadow"` keeps
running everything and reports what selecting would have skipped.

Known limits: state no project global or class reaches (a registry inside a
third-party library, the contents of an lru_cache), a name reached only by a
computed string, and the parts of a value that differ on every import.
"""

from __future__ import annotations

import ast
import enum
import functools
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import collections
import datetime
import decimal
import fractions
import tempfile
import types
import typing
import uuid
from pathlib import Path, PurePath

OUT = "PRONGS_STATE_OUT"  # a collecting session with this set writes its snapshot there
VERSION = 3
# the run-all reasons this rule answers: import-time code changed
REASONS = ("module-level change in import-time file", "executed at import/collection time",
           "collection lists the names of")
ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+|0x[0-9a-fA-F]{6,}")
CLASS_NOISE = {"__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__",
               "__static_attributes__", "__annotate_func__", "__annotations_cache__"}
PRIMITIVE = (bool, int, float, complex, str, bytes, type(None))
_BUILTIN_CALLABLES = (
    types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
    types.MethodWrapperType, types.GetSetDescriptorType, types.MemberDescriptorType,
)
# values whose repr is their whole content
VALUES = (datetime.date, datetime.time, datetime.timedelta, datetime.tzinfo, decimal.Decimal,
          fractions.Fraction, uuid.UUID, PurePath, range, slice, type(...), type(NotImplemented))
MAX_NODES = 3_000_000


def _hash(*parts: str) -> str:
    m = hashlib.blake2b(digest_size=12)
    for p in parts:
        m.update(p.encode("utf-8", "replace"))
        m.update(b"\0")
    return m.hexdigest()


def _ref(obj) -> str:
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", "?")
    return f"{getattr(obj, '__module__', '?')}.{name}"


def _prim(value) -> str:
    return f"{type(value).__name__}:{value!r}"


class Graph:
    """The objects reachable from a set of roots: one node per object, a
    token for what it is, labelled edges to what it holds. Primitives are
    folded into their holder; a class holds nothing here (it is a boundary)."""

    def __init__(self, rootpath: str):
        self.root = rootpath
        self.index: dict[int, int] = {}
        self.keep: list = []  # keeps every object alive: an id is never reused
        self.token: list[str] = []
        self.prims: list[list[tuple[str, str]]] = []
        self.edges: list[list[tuple[str, int]]] = []
        self.ordered: list[bool] = []
        self.class_key: dict[int, str] = {}   # node -> key of a project class
        self.class_obj: dict[str, type] = {}
        self._project: dict[str, str | None] = {}
        self.opaque = 0

    def module_path(self, module: str | None) -> str | None:
        """A project module's repository-relative path, else None."""
        if module not in self._project:
            f = getattr(sys.modules.get(module or ""), "__file__", None) or ""
            rel = f[len(self.root):] if f.startswith(self.root) else None
            if rel is not None and ("site-packages" in rel or rel.startswith(".")
                                    or "/." in rel):
                rel = None  # an environment or a hidden directory inside the repository
            self._project[module or ""] = rel
        return self._project[module or ""]

    def add(self, obj) -> int:
        """The node of `obj`, building the graph below it (iteratively: an
        object graph is deeper than the interpreter's stack)."""
        got = self.index.get(id(obj))
        if got is not None:
            return got
        first = self._new(obj)
        stack = [(first, obj)]
        while stack:
            node, o = stack.pop()
            try:
                token, children, ordered = self._expand(o)
            except Exception:  # a property that raises, a repr that fails: no verdict
                token, children, ordered = self._opaque(o), [], True
            self.token[node], self.ordered[node] = token, ordered
            for label, child in children:
                if isinstance(child, PRIMITIVE):
                    self.prims[node].append((label, _prim(child)))
                    continue
                c = self.index.get(id(child))
                if c is None:
                    if len(self.token) >= MAX_NODES:
                        self.prims[node].append((label, self._opaque(child)))
                        continue
                    c = self._new(child)
                    stack.append((c, child))
                self.edges[node].append((label, c))
        return first

    def _new(self, obj) -> int:
        n = len(self.token)
        self.index[id(obj)] = n
        self.keep.append(obj)
        self.token.append("")
        self.prims.append([])
        self.edges.append([])
        self.ordered.append(True)
        return n

    def _opaque(self, obj) -> str:
        """An object with nothing to look into (a lock, a compiled schema, a
        weak reference): its type is all that can be compared. Its repr is
        not: it carries addresses, and orderings that differ per process."""
        self.opaque += 1
        return f"opaque:{_ref(type(obj))}"

    def _state(self, obj) -> list[tuple[str, object]]:
        out: list[tuple[str, object]] = []
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            out += [(f".{k}", v) for k, v in d.items() if isinstance(k, str)]
        for klass in type(obj).__mro__:
            slots = klass.__dict__.get("__slots__", ())
            for s in (slots,) if isinstance(slots, str) else slots or ():
                if s not in ("__dict__", "__weakref__") and hasattr(obj, s):
                    out.append((f".{s}", getattr(obj, s)))
        return out

    def _expand(self, obj) -> tuple[str, list[tuple[str, object]], bool]:
        if isinstance(obj, types.ModuleType):
            return f"module:{obj.__name__}", [], True
        if isinstance(obj, (tuple, list, collections.deque)):
            return type(obj).__name__, [(str(i), x) for i, x in enumerate(obj)], True
        if isinstance(obj, VALUES):
            return f"value:{_ref(type(obj))}:{obj!r}", [], True
        if isinstance(obj, (set, frozenset)):
            return type(obj).__name__, [("", x) for x in obj], False
        if isinstance(obj, (dict, types.MappingProxyType)):
            children: list[tuple[str, object]] = []
            for i, (k, v) in enumerate(obj.items()):
                if isinstance(k, PRIMITIVE):
                    children.append((f"[{_prim(k)[:200]}]", v))
                else:
                    children += [(f"key{i}", k), (f"value{i}", v)]
            return type(obj).__name__, children, True
        if isinstance(obj, types.FunctionType):
            if self.module_path(obj.__module__) is None:
                return f"function:{_ref(obj)}", [], True
            # not its code: a changed body is found by the tests that enter it
            children = [("defaults", obj.__defaults__), ("kwdefaults", obj.__kwdefaults__)]
            for name, cell in zip(obj.__code__.co_freevars, obj.__closure__ or ()):
                try:
                    children.append((f"closure:{name}", cell.cell_contents))
                except ValueError:
                    pass
            return f"function:{_ref(obj)}", children + self._state(obj), True
        if isinstance(obj, _BUILTIN_CALLABLES):
            return f"builtin:{_ref(obj)}", [], True
        if isinstance(obj, types.MethodType):
            return "method", [("func", obj.__func__), ("self", obj.__self__)], True
        if isinstance(obj, (staticmethod, classmethod)):
            return type(obj).__name__, [("func", obj.__func__)], True
        if isinstance(obj, property):
            return "property", [("fget", obj.fget), ("fset", obj.fset), ("fdel", obj.fdel)], True
        if isinstance(obj, functools.partial):
            return "partial", [("func", obj.func), ("args", obj.args),
                               ("keywords", obj.keywords)], True
        if isinstance(obj, type):
            path = self.module_path(getattr(obj, "__module__", None))
            if path is not None:
                key = f"{path}::{obj.__qualname__}"
                self.class_key[self.index[id(obj)]] = key
                self.class_obj.setdefault(key, obj)
            return f"class:{_ref(obj)}", [], True  # a boundary: its attributes are roots
        if isinstance(obj, re.Pattern):
            return f"re:{obj.pattern!r}:{obj.flags}", [], True
        if isinstance(obj, enum.Enum):
            return f"enum:{obj.name}", [("class", type(obj))], True
        if isinstance(obj, (types.GenericAlias, types.UnionType)) or type(obj).__module__ == "typing":
            origin, args = typing.get_origin(obj), typing.get_args(obj)
            if origin is typing.Union or origin is types.UnionType:
                # a union built from a set of types has no order of its own
                return "union", [("", a) for a in args], False
            if args:
                return "generic", [("origin", origin), *((str(i), a) for i, a in enumerate(args))], True
            return f"typing:{ADDRESS.sub('', repr(obj))}", [], True
        state = self._state(obj)
        if state or hasattr(obj, "__dict__"):
            return "object", [("class", type(obj)), *state], True
        return self._opaque(obj), [("class", type(obj))], True

    # --- fingerprints --------------------------------------------------------
    def solve(self) -> None:
        """Fingerprint every node and collect the project classes it refers
        to. Strongly connected components (Tarjan, iterative) are taken as a
        whole, sinks first, so a change anywhere in a cycle, or below it,
        changes the fingerprint of every node that can reach it."""
        n = len(self.token)
        self.fp: list[str] = [""] * n
        self.refs: list[frozenset] = [frozenset()] * n
        low, num, on = [0] * n, [-1] * n, [False] * n
        comp = [-1] * n
        order: list[int] = []
        counter = 0
        interned: dict[frozenset, frozenset] = {}
        for start in range(n):
            if num[start] != -1:
                continue
            work = [(start, 0)]
            while work:
                v, i = work[-1]
                if i == 0:
                    num[v] = low[v] = counter
                    counter += 1
                    order.append(v)
                    on[v] = True
                edges = self.edges[v]
                if i < len(edges):
                    work[-1] = (v, i + 1)
                    w = edges[i][1]
                    if num[w] == -1:
                        work.append((w, 0))
                    elif on[w]:
                        low[v] = min(low[v], num[w])
                    continue
                work.pop()
                if work:
                    u = work[-1][0]
                    low[u] = min(low[u], low[v])
                if low[v] == num[v]:
                    members = []
                    while True:
                        w = order.pop()
                        on[w] = False
                        comp[w] = v
                        members.append(w)
                        if w == v:
                            break
                    self._solve_component(members, comp, v, interned)

    def _local(self, v: int, comp: list[int], root: int) -> str:
        parts = [f"{label}={value}" for label, value in self.prims[v]]
        parts += [f"{label}>{'~' if comp[w] == root else self.fp[w]}" for label, w in self.edges[v]]
        if not self.ordered[v]:
            parts.sort()
        return _hash(self.token[v], *parts)

    def _solve_component(self, members: list[int], comp: list[int], root: int,
                         interned: dict) -> None:
        refs: set = set()
        for v in members:
            key = self.class_key.get(v)
            if key is not None:
                refs.add(key)
            for _, w in self.edges[v]:
                if comp[w] != root:
                    refs |= self.refs[w]
        shared = interned.setdefault(frozenset(refs), frozenset(refs))
        if len(members) == 1:
            v = members[0]
            self.fp[v], self.refs[v] = self._local(v, comp, root), shared
            return
        locals_ = {v: self._local(v, comp, root) for v in members}
        whole = _hash("cycle", *sorted(locals_.values()))
        for v in members:
            self.fp[v], self.refs[v] = _hash(whole, locals_[v]), shared

    def parts(self, v: int) -> dict[str, str] | None:
        """One level of a root, by label: where volatility is narrowed to."""
        if not self.ordered[v]:
            return None
        out = {label: _hash(value) for label, value in self.prims[v]}
        out.update({label: self.fp[w] for label, w in self.edges[v]})
        return out


class _Node(int):
    """A graph node index, told apart from a primitive int value."""


def snapshot(rootpath: str) -> dict:
    """What import produced, for the project modules imported so far (see
    the module docstring): {'modules': {path: {name: fingerprint}}, 'parts':
    {path: {name: {label: fingerprint}}}, 'refs': {path: {name: refset}},
    'classes': {key: {'bases', 'attrs', 'refs'}}, 'refsets': [[class key]]}."""
    g = Graph(rootpath)
    roots: dict[str, dict[str, object]] = {}
    for name, mod in sorted(sys.modules.items()):
        path = g.module_path(name) if mod is not None else None
        if path is None:
            continue
        names = roots.setdefault(path, {})
        # __dict__, not vars(): the recorder counts vars() as a test listing names
        for k, v in list(mod.__dict__.items()):
            if k.startswith("__") and k != "__all__":
                continue
            names[k] = v if isinstance(v, PRIMITIVE) else _Node(g.add(v))
    # every project class the graph reached: its attributes are roots too
    class_nodes: dict[str, dict[str, object]] = {}
    done: set[str] = set()
    while len(done) < len(g.class_obj):
        for key in [k for k in g.class_obj if k not in done]:
            done.add(key)
            cls = g.class_obj[key]
            for base in cls.__mro__[1:]:
                g.add(base)
            class_nodes[key] = {
                k: v if isinstance(v, PRIMITIVE) else _Node(g.add(v))
                for k, v in cls.__dict__.items() if isinstance(k, str) and k not in CLASS_NOISE
            }
    g.solve()
    refsets: dict[frozenset, int] = {}

    def refset(node: int) -> int | None:
        refs = g.refs[node]
        return refsets.setdefault(refs, len(refsets)) if refs else None

    def describe(values: dict[str, object], with_parts: bool) -> tuple[dict, dict, dict]:
        fps, refs, parts = {}, {}, {}
        for k, v in values.items():
            if not isinstance(v, _Node):
                fps[k] = _hash(_prim(v))
                continue
            fps[k] = g.fp[v]
            rid = refset(v)
            if rid is not None:
                refs[k] = rid
            if with_parts:
                p = g.parts(v)
                if p:
                    parts[k] = p
        return fps, refs, parts

    out: dict = {"version": VERSION, "modules": {}, "refs": {}, "parts": {}, "classes": {},
                 "opaque": g.opaque}
    for path, values in roots.items():
        fps, refs, parts = describe(values, True)
        out["modules"][path] = fps
        if refs:
            out["refs"][path] = refs
        if parts:
            out["parts"][path] = parts
    for key, values in class_nodes.items():
        cls = g.class_obj[key]
        fps, refs, _ = describe(values, False)
        bases = []
        for base in cls.__mro__[1:]:
            path = g.module_path(getattr(base, "__module__", None))
            bases.append(f"{path}::{base.__qualname__}" if path is not None else _ref(base))
        out["classes"][key] = {"bases": bases, "attrs": fps, "refs": refs}
    out["refsets"] = [sorted(r) for r, _ in sorted(refsets.items(), key=lambda kv: kv[1])]
    return out


def _both(base: dict, now: dict) -> set[str]:
    # a module only one of the two sessions imported (they collected
    # different test modules) says nothing: compare what both imported
    return base["modules"].keys() & now["modules"].keys()


def volatile(a: dict, b: dict) -> dict:
    """What differs between two snapshots of one tree: {'globals': {path:
    {name: [labels] | None}}, 'classes': {key: [attributes]}}. Labels narrow
    it to the parts of a root that differ; None is the whole root."""
    globals_: dict[str, dict[str, list[str] | None]] = {}
    for path in _both(a, b):
        fa, fb = a["modules"][path], b["modules"][path]
        for name in fa.keys() | fb.keys():
            if fa.get(name) == fb.get(name):
                continue
            pa = a["parts"].get(path, {}).get(name)
            pb = b["parts"].get(path, {}).get(name)
            labels = None
            # narrowed to parts only where a part is the same thing in both: a
            # mapping whose keys differ, or a sequence (position is no
            # identity: one item more shifts the rest), is volatile whole
            if (pa is not None and pb is not None and pa.keys() == pb.keys()
                    and not all(k.isdigit() for k in pa)):
                labels = sorted(k for k in pa if pa[k] != pb[k])
            globals_.setdefault(path, {})[name] = labels
    classes: dict[str, list[str]] = {}
    for key in a["classes"].keys() & b["classes"].keys():
        ca, cb = a["classes"][key]["attrs"], b["classes"][key]["attrs"]
        attrs = sorted(k for k in ca.keys() | cb.keys() if ca.get(k) != cb.get(k))
        if attrs:
            classes[key] = attrs
    return {"globals": globals_, "classes": classes}


def stored(snap: dict, vol: dict) -> dict:
    """A snapshot as it is kept: with what is volatile in it, and the parts
    of the volatile roots only (they are what a later comparison needs)."""
    parts = {
        path: {name: snap["parts"][path][name] for name, labels in names.items()
               if labels is not None and name in snap["parts"].get(path, {})}
        for path, names in vol["globals"].items()
    }
    return snap | {"volatile": vol, "parts": {p: v for p, v in parts.items() if v}}


def diff(base: dict, now: dict) -> dict:
    """What import produces differently now: {'globals': [[path, name]],
    'classes': [key] (changed, or inheriting from one that did), 'names':
    the identifiers a test would use to reach any of it}."""
    vol = base.get("volatile") or {"globals": {}, "classes": {}}
    both = _both(base, now)
    changed: list[list[str]] = []
    for path in sorted(both):
        fa, fb = base["modules"][path], now["modules"][path]
        skip = vol["globals"].get(path, {})
        for name in sorted(fa.keys() | fb.keys()):
            if fa.get(name) == fb.get(name):
                continue
            if name in skip:
                labels = skip[name]
                pa = base["parts"].get(path, {}).get(name)
                pb = now["parts"].get(path, {}).get(name)
                if labels is None or pa is None or pb is None:
                    continue  # differs on every import: nothing to compare
                if all(pa.get(k) == pb.get(k) for k in pa.keys() | pb.keys() if k not in labels):
                    continue
            changed.append([path, name])
    names = {name for _, name in changed}

    classes: set[str] = set()
    keys = base["classes"].keys() | now["classes"].keys()
    for key in keys:
        if key.split("::", 1)[0] not in both:
            continue
        a, b = base["classes"].get(key), now["classes"].get(key)
        if a is None or b is None:
            classes.add(key)
            continue
        skip_attrs = set(vol["classes"].get(key, ()))
        if a["bases"] != b["bases"] or any(
                a["attrs"].get(k) != b["attrs"].get(k)
                for k in a["attrs"].keys() | b["attrs"].keys() if k not in skip_attrs):
            classes.add(key)
    # a subclass inherits the change; a class holding a changed one under a
    # dunder (looked up by the interpreter, not by name) is changed with it
    while True:
        grown = set(classes)
        for snap in (base, now):
            hit = [bool(classes.intersection(r)) for r in snap["refsets"]]
            for key, c in snap["classes"].items():
                if key in grown or key.split("::", 1)[0] not in both:
                    continue
                if classes.intersection(c["bases"]) or any(
                        hit[rid] for attr, rid in c["refs"].items()
                        if attr.startswith("__") and attr.endswith("__")):
                    grown.add(key)
        if grown == classes:
            break
        classes = grown
    names |= {key.split("::", 1)[1].rsplit(".", 1)[-1] for key in classes}
    # whatever holds a changed class, or an instance of one, by a name
    holders = 0
    for snap in (base, now):
        hit = [bool(classes.intersection(r)) for r in snap["refsets"]]
        for path, refs in snap["refs"].items():
            if path in both:
                for name, rid in refs.items():
                    if hit[rid] and name not in names:
                        names.add(name)
                        holders += 1
        for key, c in snap["classes"].items():
            if key.split("::", 1)[0] in both:
                for attr, rid in c["refs"].items():
                    if hit[rid] and not (attr.startswith("__") and attr.endswith("__")):
                        if attr not in names:
                            names.add(attr)
                            holders += 1
    return {"globals": changed, "classes": sorted(classes), "names": sorted(names),
            "holders": holders}


def summary(change: dict) -> dict:
    """A change, for a result: counts, and the first names."""
    return {"globals": len(change["globals"]), "classes": len(change["classes"]),
            "holders": change["holders"], "names": change["names"][:20]}


def function_names(source: str) -> dict[str, set[str]]:
    """qualname -> every identifier its body mentions: names, attributes and
    strings that are identifiers (getattr(x, "name"))."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    out: dict[str, set[str]] = {}

    def walk(node, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + child.name
                names = out.setdefault(qual, set())
                for n in ast.walk(child):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
                    elif isinstance(n, ast.Attribute):
                        names.add(n.attr)
                    elif (isinstance(n, ast.Constant) and isinstance(n.value, str)
                          and n.value.isidentifier()):
                        names.add(n.value)
                walk(child, f"{qual}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def reach(con, repo: Path, change: dict) -> set[str]:
    """The tests that entered a function naming something that changed, or a
    method of a changed class. A file that cannot be read reaches everything
    that entered it."""
    names = set(change["names"])
    classes: dict[str, set[str]] = {}
    for cls in change["classes"]:
        path, qual = cls.split("::", 1)
        classes.setdefault(path, set()).add(qual)
    # a changed global that is a function (a default, a closure, a mark): the
    # tests that enter it
    for path, name in change["globals"]:
        classes.setdefault(path, set())
    own: dict[str, set[str]] = {}
    for path, name in change["globals"]:
        own.setdefault(path, set()).add(name)
    by_file: dict[str, list[tuple[int, str]]] = {}
    for fid, path, qual in con.execute(
            "SELECT fn.id, f.path, fn.qualname FROM funcs fn JOIN files f ON f.id = fn.file_id"):
        by_file.setdefault(path, []).append((fid, qual))
    hit: list[int] = []
    for path, funcs in by_file.items():
        if not path.endswith(".py"):
            continue
        try:
            mentioned = function_names((repo / path).read_text())
        except (OSError, UnicodeDecodeError):
            hit += [fid for fid, _ in funcs]
            continue
        here = classes.get(path, set())
        mine = own.get(path, set())
        for fid, qual in funcs:
            if qual not in mentioned:
                continue  # a module or class body: it ran at import
            if (mentioned[qual] & names or qual.split(".", 1)[0] in mine
                    or any(qual.startswith(c + ".") for c in here)):
                hit.append(fid)
    tests: set[str] = set()
    for i in range(0, len(hit), 500):
        chunk = hit[i:i + 500]
        tests |= {t for (t,) in con.execute(
            "SELECT DISTINCT t.test_id FROM tests t JOIN current_links l ON l.test_id = t.id "
            f"WHERE l.func_id IN ({','.join('?' * len(chunk))})", chunk)}
    return tests


def state_dir(repo: Path) -> Path:
    from prongs import journal

    return journal.state_dir(repo) / "state"


def save(repo: Path, key: str, snap: dict) -> Path:
    d = state_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{key}.json"
    path.write_text(json.dumps(snap))
    return path


def load(repo: Path, key: str) -> dict | None:
    try:
        snap = json.loads((state_dir(repo) / f"{key}.json").read_text())
    except (OSError, ValueError):
        return None
    return snap if snap.get("version") == VERSION else None


def prune(repo: Path, keep: set[str]) -> int:
    """Delete the snapshots of runs that are no longer evidence; the number
    deleted."""
    n = 0
    for f in state_dir(repo).glob("*.json"):
        if f.stem not in keep:
            f.unlink(missing_ok=True)
            n += 1
    return n


def suite_modules(repo: Path, extra=()) -> list[str]:
    """The test modules a snapshot collects: the ones the map knows (and that
    still exist) and `extra`, the session's own. A session that ran three
    tests then describes the same imports a full run does, so its snapshot
    can be compared with one."""
    from prongs import journal, mapdb

    repo = Path(repo)
    mods = set(extra)
    db = journal.map_path(repo)
    if db.exists():
        try:
            con = mapdb.connect(str(db))
            try:
                known = {t.split("::", 1)[0] for (t,) in con.execute(
                    "SELECT test_id FROM tests WHERE retired_run IS NULL "
                    "AND last_run IS NOT NULL AND test_id != ?", (mapdb.COLLECTION,))}
            finally:
                con.close()
        except sqlite3.DatabaseError:
            known = set()
        mods |= {m for m in known if (repo / m).exists()}
    return sorted(mods)


def record(rootpath: str | Path, key: str, modules: list[str]) -> Path | None:
    """Snapshot what import produces in this tree and store it under a run's
    journal key. Two fresh processes, side by side: what differs between two
    imports of one tree says nothing about a change."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(2) as pool:
        first, again = (r["snapshot"] for r in pool.map(
            lambda _: collect(rootpath, modules), range(2)))
    if first is None or again is None:
        return None
    return save(Path(rootpath), key, stored(first, volatile(first, again)))


def collect(rootpath: str | Path, modules: list[str]) -> dict:
    """Collect test modules in a fresh process, running nothing: {'ok',
    'exit', 'output' (on failure), 'snapshot' (what import produced there)}."""
    env = {k: v for k, v in os.environ.items() if k != "PRONGS_COV"}
    # one hash seed for every snapshot: the order of a set, and of whatever is
    # built by iterating one, is then the same in each, and only what really
    # differs from import to import (a clock, a pid, an address) is volatile
    env["PYTHONHASHSEED"] = "0"
    with tempfile.TemporaryDirectory(prefix="prongs-state-") as tmp:
        env[OUT] = os.path.join(tmp, "snapshot.json")
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             *shlex.split(os.environ.get("PRONGS_RUN_ARGS", "")), *modules],
            capture_output=True, text=True, env=env, cwd=rootpath,
        )
        try:
            snap = json.loads(Path(env[OUT]).read_text())
        except (OSError, ValueError):
            snap = None
    ok = p.returncode in (0, 5)  # 5: nothing collected
    out = {"ok": ok, "exit": p.returncode, "snapshot": snap}
    if not ok:
        out["output"] = p.stdout[-1500:]
    return out
