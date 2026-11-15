"""The recorder inside a child process.

prongs.pth imports this module in every Python process whose environment
carries PRONGS_CHILD (set by a recording session): the functions the child
enters are written, as it first enters each, to one report for the test
named in PRONGS_TEST. Written as it goes, not when it exits: a server a test
starts and then kills never gets to exit, and still said what it ran.

A report is lines of JSON: a header ({"test", "pid", "parent"}), then one
[file, qualname, line] per function. A process that cannot write it leaves
none, and the test that started it is treated as having started a process
that did not report (see evidence.py).

A pytest session has a recorder of its own and is left alone.
"""

from __future__ import annotations

import json
import os
import sys
import threading


def _start() -> None:
    out, root = os.environ.get("PRONGS_CHILD"), os.environ.get("PRONGS_CHILD_ROOT")
    if not out or not root or not os.path.isdir(out):
        return
    known: dict = {}
    pid = os.getpid()
    # line-buffered and appending: each function is one write, whole even
    # when a fork of this child writes to the same report
    report = open(os.path.join(out, f"{pid}.jsonl"), "a", buffering=1)
    report.write(json.dumps({"test": os.environ.get("PRONGS_TEST"), "pid": pid,
                             "parent": os.getppid()}) + "\n")

    def first(code) -> bool:
        """Whether this is project code seen for the first time (and note it)."""
        k = known.get(code)
        if k is None:
            fn = code.co_filename
            k = known[code] = fn.startswith(root) and "site-packages" not in fn
            if k:
                try:
                    report.write(json.dumps(
                        [fn, code.co_qualname, code.co_firstlineno]) + "\n")
                except (OSError, ValueError):
                    pass
        return k

    if hasattr(sys, "monitoring"):
        mon = sys.monitoring
        tool = mon.DEBUGGER_ID  # a recording pytest in this process takes COVERAGE_ID
        try:
            mon.use_tool_id(tool, "prongs-child")
        except ValueError:
            return

        def on_start(code, offset):
            first(code)
            return mon.DISABLE  # once per function is all a report needs

        mon.register_callback(tool, mon.events.PY_START, on_start)
        mon.set_events(tool, mon.events.PY_START)
    else:
        def profile(frame, event, arg):
            if event == "call":
                first(frame.f_code)

        sys.setprofile(profile)
        threading.setprofile(profile)


try:
    _start()
except Exception:  # a broken recorder must never break the program it rides in
    pass
