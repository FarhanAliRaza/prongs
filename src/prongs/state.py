"""What import leaves behind, fingerprinted: an experimental rule, in shadow.

A change to code that runs at import (a module-level statement, a class
attribute, a function a module body calls) runs everything, because function
entry cannot say which tests use the objects that code built. This module
asks the other question, Salsa's: did what import *produced* change?

  snapshot()   after collection, a fingerprint of every global of every
               project module, and of every attribute of every project
               class: its value, followed through containers, instances,
               closures and classes. Functions are fingerprinted without
               their code (a changed body is the map's business), so a
               fingerprint moves only when import-time state does.
  diff()       the globals and class attributes whose fingerprint differs
               between two snapshots, ignoring those that differ between two
               snapshots of one tree (a timestamp, an id, an unordered set).
  reach()      the tests that could see a changed name: those that entered a
               function whose source names it, or a method of a changed
               class.

Nothing selects by this yet. With `import_state = true` in [tool.prongs] a
recorded run stores a snapshot, and a `prongs run` that executes everything
for an import-time reason reports what this rule would have skipped and
whether any skipped test changed outcome (the `import_state` rule of the
shadow report). It is promoted when that log says it can be.

Known limits: state that no project global reaches (a registry inside a
third-party library), a name reached only by a computed string, and an
object handed over at run time by code that names neither it nor its class.
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
import subprocess
import sys
import tempfile
import types
from pathlib import Path

OUT = "PRONGS_STATE_OUT"  # a collecting session with this set writes its snapshot there

ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+|0x[0-9a-fA-F]{6,}")
CLASS_NOISE = {"__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__",
               "__static_attributes__", "__annotate_func__", "__annotations_cache__"}
_BUILTIN_CALLABLES = (
    types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
    types.MethodWrapperType, types.GetSetDescriptorType, types.MemberDescriptorType,
)
_PENDING = object()


def _hash(*parts: str) -> str:
    m = hashlib.blake2b(digest_size=12)
    for p in parts:
        m.update(p.encode("utf-8", "replace"))
        m.update(b"\0")
    return m.hexdigest()


def _ref(obj) -> str:
    name = getattr(obj, "__qualname__", None) or getattr(obj, "__name__", "?")
    return f"{getattr(obj, '__module__', '?')}.{name}"


class Fingerprinter:
    """Fingerprints of live objects, memoized by identity for one snapshot."""

    def __init__(self, rootpath: str):
        self.root = rootpath
        self.memo: dict[int, object] = {}
        self.keep: list = []  # keeps fingerprinted objects alive: an id is never reused
        self.project: dict[str, bool] = {}
        self.opaque = 0

    def is_project(self, module: str | None) -> bool:
        got = self.project.get(module or "")
        if got is None:
            f = getattr(sys.modules.get(module or ""), "__file__", None) or ""
            got = self.project[module or ""] = (
                f.startswith(self.root) and "site-packages" not in f)
        return got

    def of(self, obj, depth: int = 0) -> str:
        if obj is None or isinstance(obj, (bool, int, float, complex, str, bytes)):
            return _hash(type(obj).__name__, repr(obj))
        key = id(obj)
        got = self.memo.get(key)
        if got is _PENDING:
            return _hash("cycle", _ref(type(obj)))
        if got is not None:
            return got  # type: ignore[return-value]
        self.memo[key] = _PENDING
        self.keep.append(obj)
        try:
            out = self._of(obj, depth + 1) if depth < 80 else self._opaque(obj)
        except Exception:  # a property that raises, a repr that fails: no verdict
            out = self._opaque(obj)
        self.memo[key] = out
        return out

    def _opaque(self, obj) -> str:
        self.opaque += 1
        try:
            text = ADDRESS.sub("", repr(obj))[:500]
        except Exception:
            text = "?"
        return _hash("opaque", _ref(type(obj)), text)

    def _state(self, obj, depth: int) -> list[str]:
        parts = []
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            parts += [f"{k}={self.of(v, depth)}" for k, v in d.items() if isinstance(k, str)]
        for klass in type(obj).__mro__:
            slots = klass.__dict__.get("__slots__", ())
            for s in (slots,) if isinstance(slots, str) else slots or ():
                if s not in ("__dict__", "__weakref__") and hasattr(obj, s):
                    parts.append(f"{s}={self.of(getattr(obj, s), depth)}")
        return parts

    def _of(self, obj, depth: int) -> str:
        of = self.of
        if isinstance(obj, types.ModuleType):
            return _hash("module", obj.__name__)
        if isinstance(obj, (tuple, list)):
            return _hash(type(obj).__name__, *[of(x, depth) for x in obj])
        if isinstance(obj, (set, frozenset)):
            return _hash(type(obj).__name__, *sorted(of(x, depth) for x in obj))
        if isinstance(obj, dict):
            return _hash(type(obj).__name__,
                         *[f"{of(k, depth)}:{of(v, depth)}" for k, v in obj.items()])
        if isinstance(obj, types.FunctionType):
            parts = ["function", _ref(obj)]
            if self.is_project(obj.__module__):
                # not its code: a changed body is found by the tests that enter it
                parts += [of(obj.__defaults__, depth), of(obj.__kwdefaults__, depth)]
                for cell in obj.__closure__ or ():
                    try:
                        parts.append(of(cell.cell_contents, depth))
                    except ValueError:
                        parts.append("empty")
                parts += self._state(obj, depth)
            return _hash(*parts)
        if isinstance(obj, _BUILTIN_CALLABLES):
            return _hash("builtin", _ref(obj))
        if isinstance(obj, types.MethodType):
            return _hash("method", of(obj.__func__, depth), of(obj.__self__, depth))
        if isinstance(obj, (staticmethod, classmethod)):
            return _hash(type(obj).__name__, of(obj.__func__, depth))
        if isinstance(obj, property):
            return _hash("property", of(obj.fget, depth), of(obj.fset, depth), of(obj.fdel, depth))
        if isinstance(obj, functools.partial):
            return _hash("partial", of(obj.func, depth), of(obj.args, depth),
                         of(obj.keywords, depth))
        if isinstance(obj, type):
            if self.is_project(getattr(obj, "__module__", None)):
                # by value: a changed class changes whatever holds it
                return _hash("class", *self.class_attrs(obj, depth).values(),
                             _ref(type(obj)), *[of(b, depth) for b in obj.__bases__])
            return _hash("class", _ref(obj))
        if isinstance(obj, re.Pattern):
            return _hash("re", repr(obj.pattern), str(obj.flags))
        if isinstance(obj, enum.Enum):
            return _hash("enum", of(type(obj), depth), obj.name)
        if isinstance(obj, (types.GenericAlias, types.UnionType)) or type(obj).__module__ == "typing":
            return _hash("typing", ADDRESS.sub("", repr(obj)))
        parts = self._state(obj, depth)
        if parts or hasattr(obj, "__dict__"):
            return _hash("object", of(type(obj), depth), *parts)
        return self._opaque(obj)

    def class_attrs(self, cls, depth: int = 0) -> dict[str, str]:
        # __dict__, not vars(): the recorder counts vars() as a test listing names
        return {k: self.of(v, depth) for k, v in cls.__dict__.items()
                if isinstance(k, str) and k not in CLASS_NOISE}


def snapshot(rootpath: str) -> dict:
    """{'modules': {path: {name: fingerprint}}, 'classes': {path::Class:
    {attribute: fingerprint}}} for the project modules imported so far."""
    fp = Fingerprinter(rootpath)
    modules: dict[str, dict[str, str]] = {}
    classes: dict[str, dict[str, str]] = {}
    for name, mod in sorted(sys.modules.items()):
        if mod is None or not fp.is_project(name):
            continue
        rel = os.path.relpath(mod.__file__, rootpath)
        names = modules.setdefault(rel, {})
        for k, v in list(mod.__dict__.items()):
            if k.startswith("__") and k != "__all__":
                continue
            names[k] = fp.of(v)
            if isinstance(v, type) and v.__module__ == name:
                classes[f"{rel}::{v.__qualname__}"] = fp.class_attrs(v)
    return {"modules": modules, "classes": classes, "opaque": fp.opaque}


def _flat(snap: dict) -> dict[tuple[str, str], str]:
    return {(m, k): v for m, names in snap["modules"].items() for k, v in names.items()}


def volatile(a: dict, b: dict) -> list[list[str]]:
    """The globals that differ between two snapshots of one tree."""
    fa, fb = _flat(a), _flat(b)
    return sorted([m, k] for m, k in fa.keys() | fb.keys() if fa.get((m, k)) != fb.get((m, k)))


def diff(base: dict, now: dict) -> dict:
    """What import produced differently: {'globals': [[path, name]],
    'classes': [path::Class], 'names': the identifiers a test would use to
    reach any of it}."""
    skip = {tuple(v) for v in base.get("volatile", [])}
    fa, fb = _flat(base), _flat(now)
    changed = sorted(k for k in fa.keys() | fb.keys() if k not in skip and fa.get(k) != fb.get(k))
    names = {k for _, k in changed}
    classes = []
    for cls in sorted(base["classes"].keys() | now["classes"].keys()):
        path, qual = cls.split("::", 1)
        if (path, qual.split(".")[0]) in skip:
            continue
        a, b = base["classes"].get(cls, {}), now["classes"].get(cls, {})
        attrs = {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
        if attrs:
            classes.append(cls)
            names |= {k for k in attrs if not (k.startswith("__") and k.endswith("__"))}
    return {"globals": [list(k) for k in changed], "classes": classes, "names": sorted(names)}


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
        for fid, qual in funcs:
            if qual not in mentioned:
                continue  # a module or class body: it ran at import
            if mentioned[qual] & names or any(qual.startswith(c + ".") for c in here):
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
        return json.loads((state_dir(repo) / f"{key}.json").read_text())
    except (OSError, ValueError):
        return None


def collect(rootpath: str | Path, modules: list[str]) -> dict:
    """Collect test modules in a fresh process, running nothing: {'ok',
    'exit', 'output' (on failure), 'snapshot' (what import produced there)}."""
    env = {k: v for k, v in os.environ.items() if k != "PRONGS_COV"}
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
