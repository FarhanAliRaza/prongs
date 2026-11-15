"""Which project files any process opens, as the kernel sees it (inotify).

The recorder's own hooks see what the test process does in Python: the
functions it enters, the files it opens with open(). They cannot see into a
program that is not Python (git, node, a linter), a Python that runs without
the recorder, or a C library that opens a file itself. On Linux the kernel
can say which files under the project were opened while a test ran, by
whichever process:

  a tracked file              the test depends on that file
  a module's compiled file    (__pycache__/x.cpython-NN.pyc) the same, for x.py
  a directory                 the test listed it: it can see a file appear
                              or disappear there
  .git/HEAD, .git/index       the test read the repository's own state,
                              which no list of files describes

So a test that starts such a program depends on what was opened, not on
every change there could be (evidence.py turns this into dependencies).

What this cannot see: a program that only asks for a file's metadata (stat:
`git status` on an unchanged file, `make` comparing times) opens nothing.
Events the kernel dropped (its queue was full) are reported as lost, and the
test keeps the marker it had without a watcher.

Linux only: `available()` is False elsewhere, or with PRONGS_WATCH=0.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys

IN_OPEN, IN_CREATE, IN_Q_OVERFLOW, IN_ISDIR = 0x20, 0x100, 0x4000, 0x40000000
IN_ONLYDIR = 0x01000000
_HEADER = struct.Struct("iIII")
GIT_STATE = {"HEAD", "index"}
CACHE = "__pycache__"


def available() -> bool:
    return sys.platform.startswith("linux") and os.environ.get("PRONGS_WATCH") != "0"


def source_of(compiled: str) -> str:
    """x.cpython-314.pyc, x.cpython-314.opt-1.pyc -> x.py"""
    return compiled.split(".", 1)[0] + ".py"


class Watcher:
    """Watches the directories that hold tracked files. `root` ends with a
    separator; `tracked` are paths relative to it."""

    def __init__(self, root: str, tracked: set[str]):
        self.root = root
        self._libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self._libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self._watches: dict[int, tuple[str, str]] = {}  # wd -> (relative dir, kind)
        self.dirs: set[str] = set()
        for path in tracked:
            d = os.path.dirname(path)
            while d not in self.dirs:
                self.dirs.add(d)
                d = os.path.dirname(d)
        for d in self.dirs:
            if self._add(d, "dir"):
                self._add(os.path.join(d, CACHE), "cache")
        self._add(".git", "git")

    def _add(self, rel: str, kind: str) -> bool:
        mask = IN_OPEN | IN_ONLYDIR | (IN_CREATE if kind == "dir" else 0)
        wd = self._libc.inotify_add_watch(self.fd, os.fsencode(self.root + rel), mask)
        if wd < 0:
            return False  # not there (a tracked file's directory was deleted), or out of watches
        self._watches[wd] = (rel, kind)
        return True

    def drain(self) -> tuple[set[str], set[str], bool, bool]:
        """What was opened since the last call: (files, directories, the
        repository's git state was read, events were lost). Paths are
        relative to the root; a compiled module counts as its source."""
        files: set[str] = set()
        dirs: set[str] = set()
        repo = lost = False
        while True:
            try:
                buf = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                lost = True
                break
            pos = 0
            while pos < len(buf):
                wd, mask, _, length = _HEADER.unpack_from(buf, pos)
                pos += _HEADER.size
                name = os.fsdecode(buf[pos:pos + length].rstrip(b"\0"))
                pos += length
                if mask & IN_Q_OVERFLOW:
                    lost = True
                    continue
                where = self._watches.get(wd)
                if where is None:
                    continue
                rel, kind = where
                if kind == "git":
                    repo = repo or name in GIT_STATE
                elif kind == "cache":
                    if name.endswith(".pyc"):
                        files.add(os.path.join(os.path.dirname(rel), source_of(name)))
                elif mask & IN_CREATE:
                    if mask & IN_ISDIR and name == CACHE:
                        self._add(os.path.join(rel, CACHE), "cache")
                elif mask & IN_ISDIR:
                    target = os.path.join(rel, name) if name else rel
                    if target in self.dirs:
                        dirs.add(target)
                else:
                    files.add(os.path.join(rel, name))
        return files, dirs, repo, lost

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def start(root: str, tracked: set[str]) -> Watcher | None:
    """A watcher, or None where there is none to be had."""
    if not available():
        return None
    try:
        return Watcher(root, tracked)
    except (OSError, AttributeError):
        return None
