"""The recorder inside a child process.

prongs.pth imports this module in every Python process whose environment
carries PRONGS_CHILD (set by a recording session): the functions the child
enters are written, when it exits, as one report for the test named in
PRONGS_TEST. A child that never gets to exit normally writes nothing, and the
test that started it keeps its "<spawn>" marker (see evidence.py).

A pytest session has a recorder of its own and is left alone.
"""

from __future__ import annotations

import atexit
import json
import os
import sys
import threading


def _start() -> None:
    out, root = os.environ.get("PRONGS_CHILD"), os.environ.get("PRONGS_CHILD_ROOT")
    if not out or not root or not os.path.isdir(out):
        return
    seen: set = set()
    known: dict = {}
    parent, pid = os.getppid(), os.getpid()

    def project(code) -> bool:
        k = known.get(code)
        if k is None:
            fn = code.co_filename
            k = known[code] = fn.startswith(root) and "site-packages" not in fn
        return k

    if hasattr(sys, "monitoring"):
        mon = sys.monitoring
        tool = mon.DEBUGGER_ID  # a recording pytest in this process takes COVERAGE_ID
        try:
            mon.use_tool_id(tool, "prongs-child")
        except ValueError:
            return

        def on_start(code, offset):
            if not project(code):
                return mon.DISABLE
            seen.add(code)

        mon.register_callback(tool, mon.events.PY_START, on_start)
        mon.set_events(tool, mon.events.PY_START)
    else:
        def profile(frame, event, arg):
            if event == "call" and project(frame.f_code):
                seen.add(frame.f_code)

        sys.setprofile(profile)
        threading.setprofile(profile)

    def flush() -> None:
        if os.getpid() != pid:
            return  # a fork of this child: its parent reports
        report = {
            "test": os.environ.get("PRONGS_TEST"), "pid": pid, "parent": parent,
            "funcs": sorted({(c.co_filename, c.co_qualname, c.co_firstlineno) for c in seen}),
        }
        tmp = os.path.join(out, f".{pid}.tmp")
        try:
            with open(tmp, "w") as f:
                json.dump(report, f)
            os.replace(tmp, os.path.join(out, f"{pid}.json"))
        except OSError:
            pass

    atexit.register(flush)


try:
    _start()
except Exception:  # a broken recorder must never break the program it rides in
    pass
