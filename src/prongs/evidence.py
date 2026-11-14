"""Evidence beyond "this test entered that function".

Function entry is what the map is built from, and it cannot see four ways a
test comes to depend on the tree. Each is recorded here as a pseudo-function
in the test's dependency set, so the journal, the roll-up and the selector
carry it like any other dependency:

  (<path>, "<file>")         the test opened a tracked non-Python file. A
                             change to that file selects the tests that read
                             it, instead of running everything.
  (<path>, "<names>")        the test listed a module's names (dir(), vars(),
  (<path>, "<names:Class>")  inspect.getmembers): it can see a name appear or
                             disappear without ever looking it up.
  ("<process>", "<spawn>")   the test started a process whose code prongs
                             could not record (a program that is not Python,
                             a child that was killed, a fork): any change to
                             production code selects it.
  ("<process>", "<native>")  the same, for a program named in [tool.prongs]
                             native_commands: only a changed non-Python file
                             selects it.

Python child processes are recorded too (child.py, started by prongs.pth):
the functions a child enters are credited to the test that started it, and a
test whose every direct child reported back carries no <spawn> marker.

The hooks are process-wide (an audit hook cannot be removed) and do nothing
unless a recorder is active.
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

from prongs import journal

FILE, NAMES, PROCESS, SPAWN, NATIVE = "<file>", "<names>", "<process>", "<spawn>", "<native>"
CHILD_DIR, CHILD_ROOT, CHILD_TEST = "PRONGS_CHILD", "PRONGS_CHILD_ROOT", "PRONGS_TEST"
SPAWN_EVENTS = {"subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn",
                "os.exec", "os.system", "os.startfile"}
SKIP_DIRS = (".git/", "__pycache__/", ".pytest_cache/")

_active: "Evidence | None" = None
_installed = False


def names_dep(qualname: str | None = None) -> str:
    return NAMES if not qualname else f"<names:{qualname}>"


def describe(path: str, qual: str) -> str | None:
    """A selection reason for a pseudo-function, or None for a real one."""
    if qual == FILE:
        return f"reads changed file {path}"
    if qual == NAMES or qual.startswith("<names:"):
        return f"lists the names of {path}" + (f" ({qual[7:-1]})" if qual != NAMES else "")
    if qual == SPAWN:
        return "starts a process prongs could not record"
    if qual == NATIVE:
        return "starts a native program that may read the changed file"
    return None


def _audit(event: str, args: tuple) -> None:
    ev = _active
    if ev is None:
        return
    try:
        if event == "open":
            ev.opened(args[0], args[1])
        elif event in SPAWN_EVENTS:
            ev.spawned(event, args)
    except Exception:  # never let bookkeeping break the program's own call
        pass


def _wrap(name: str):
    original = getattr(builtins, name)

    def wrapper(*args):
        ev = _active
        if ev is not None and args:
            try:
                ev.listed(args[0], instance=name == "dir")
            except Exception:
                pass
        return original(*args)

    wrapper.__name__ = wrapper.__qualname__ = name
    wrapper.__doc__ = original.__doc__
    wrapper.__wrapped__ = original
    return wrapper


def _install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    sys.addaudithook(_audit)
    builtins.dir = _wrap("dir")
    builtins.vars = _wrap("vars")
    try:  # the spawn and forkserver start methods bypass the audited calls
        from multiprocessing import util

        inner = util.spawnv_passfds

        def spawnv_passfds(path, args, passfds):
            ev = _active
            if ev is not None:
                ev.spawns += 1
            return inner(path, args, passfds)

        util.spawnv_passfds = spawnv_passfds
    except Exception:
        pass


class Evidence:
    """The recorder's side: collects pseudo-dependencies for the context
    (collection, a test, a shared fixture's setup) that is current."""

    def __init__(self, rootpath: str, tracked: set[str], native: tuple[str, ...] = ()):
        self.root = rootpath
        self.tracked = tracked
        self.native = set(native)
        self.deps: set[tuple[str, str, int]] = set()
        self.spawns = 0
        self._modules: dict[str, str | None] = {}
        self.child_dir = tempfile.mkdtemp(prefix="prongs-child-")
        self._env = {k: os.environ.get(k) for k in (CHILD_DIR, CHILD_ROOT, CHILD_TEST)}
        os.environ[CHILD_DIR] = self.child_dir
        os.environ[CHILD_ROOT] = rootpath
        os.environ[CHILD_TEST] = journal.COLLECTION

    def start(self) -> None:
        global _active
        _install()
        _active = self

    def stop(self) -> None:
        global _active
        if _active is self:
            _active = None
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test(self, nodeid: str) -> None:
        self.deps, self.spawns = set(), 0
        os.environ[CHILD_TEST] = nodeid

    # --- what the hooks report ---------------------------------------------
    def opened(self, path, mode) -> None:
        if isinstance(path, int) or (isinstance(mode, str) and "r" not in mode and "+" not in mode):
            return  # a descriptor, or a write that reads nothing
        path = os.fsdecode(path)
        if path.endswith((".py", ".pyc")):
            return
        path = os.path.abspath(path)
        if path.startswith(self.root) and path[len(self.root):] in self.tracked:
            self.deps.add((path, FILE, 0))

    def spawned(self, event: str, args: tuple) -> None:
        program = None
        if event == "subprocess.Popen":
            program = args[0] or (args[1] if isinstance(args[1], (str, bytes)) else
                                  args[1][0] if args[1] else None)
        elif event in ("os.posix_spawn", "os.exec"):
            program = args[0]
        if program is not None and os.path.basename(os.fsdecode(program)) in self.native:
            self.deps.add((self.root + PROCESS, NATIVE, 0))
        else:
            self.spawns += 1

    def _module_file(self, name: str | None) -> str | None:
        if name not in self._modules:
            f = getattr(sys.modules.get(name or ""), "__file__", None) or ""
            ok = f.startswith(self.root) and "site-packages" not in f
            self._modules[name] = f if ok else None
        return self._modules[name]

    def listed(self, obj, instance: bool) -> None:
        if isinstance(obj, types.ModuleType):
            f = self._module_file(obj.__name__)
            if f:
                self.deps.add((f, NAMES, 0))
            return
        if not isinstance(obj, type):
            if not instance:
                return  # vars(instance) is the instance's own state
            obj = type(obj)
        for klass in obj.__mro__:
            f = self._module_file(getattr(klass, "__module__", None))
            if f:
                self.deps.add((f, names_dep(klass.__qualname__), 0))

    # --- child processes ----------------------------------------------------
    def children(self) -> dict[str, list[dict]]:
        """test id -> the reports of the Python processes started under it."""
        out: dict[str, list[dict]] = {}
        for p in Path(self.child_dir).glob("*.json"):
            try:
                report = json.loads(p.read_text())
            except (OSError, ValueError):
                continue  # killed while writing: no evidence
            out.setdefault(report.get("test") or "", []).append(report)
        return out

    def cleanup(self) -> None:
        shutil.rmtree(self.child_dir, ignore_errors=True)

    def spawn_marker(self) -> tuple[str, str, int]:
        return (self.root + PROCESS, SPAWN, 0)
