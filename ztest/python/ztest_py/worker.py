"""Worker-side execution loop (Milestones 1 and 4).

A worker is a fork of the pytest host taken *after* collection. It inherits
the fully configured pytest session — plugins loaded, assertions rewritten,
items collected — and executes assigned test indexes through the real
``pytest_runtest_protocol`` hook, which performs fixture setup, call,
teardown and report construction.

The ``nextitem`` contract: pytest uses ``nextitem`` to decide exactly which
fixtures to tear down after a test. The worker therefore never starts a test
until it either has the following test queued or has been told
``NO_MORE_TESTS``; the final test runs with ``nextitem=None``. Unrelated
one-item batches with ``nextitem=None`` would tear down high-scope fixtures
after every test, so the controller keeps one test of lookahead in every
worker queue.
"""

from __future__ import annotations

import os
import sys
import time
from collections import deque
from typing import Any

import pytest

from .protocol import Connection, ConnectionClosed, MessageType, ProtocolError
from .reporting import serialize_report


class WorkerReportRelay:
    """Registered inside the worker; streams runtest reports to the controller."""

    def __init__(self, conn: Connection, worker_id: int) -> None:
        self.conn = conn
        self.worker_id = worker_id
        self.current_test_id: int | None = None
        self._config: pytest.Config | None = None

    def pytest_configure(self, config: pytest.Config) -> None:
        self._config = config

    def pytest_runtest_logstart(self, nodeid: str, location: tuple) -> None:
        self.conn.send(
            MessageType.TEST_STARTED,
            {
                "worker_id": self.worker_id,
                "test_id": self.current_test_id,
                "nodeid": nodeid,
                "location": list(location),
            },
        )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        assert self._config is not None
        wasxfail = getattr(report, "wasxfail", None) is not None
        self.conn.send(
            MessageType.TEST_REPORT,
            {
                "worker_id": self.worker_id,
                "test_id": self.current_test_id,
                "nodeid": report.nodeid,
                "phase": report.when,
                "outcome": report.outcome,
                "wasxfail": wasxfail,
                "duration_ns": int(report.duration * 1e9),
                # Pre-rendered failure text so a non-Python controller (the
                # Zig slice) can print failures without decoding longrepr.
                "longrepr_text": str(report.longrepr) if report.failed else None,
                "serialized_report": serialize_report(self._config, report),
            },
        )

    def pytest_runtest_logfinish(self, nodeid: str, location: tuple) -> None:
        self.conn.send(
            MessageType.TEST_FINISHED,
            {
                "worker_id": self.worker_id,
                "test_id": self.current_test_id,
                "nodeid": nodeid,
                "location": list(location),
            },
        )


def prepare_forked_worker(config: pytest.Config, conn: Connection, worker_id: int) -> WorkerReportRelay:
    """Adjust inherited pytest state so a forked child is a quiet worker.

    * The terminal reporter is unregistered: report rendering belongs to the
      parent (or the Zig controller); worker stdout must never carry it.
    * Global capture is restarted so this worker gets fresh capture tmpfiles
      instead of sharing inherited file offsets with sibling workers.
    * The report relay streams every runtest phase over the control socket,
      which is a dedicated fd — never mixed with stdout/stderr.
    """
    pm = config.pluginmanager

    terminal = pm.getplugin("terminalreporter")
    if terminal is not None:
        pm.unregister(terminal)

    # The oracle (and any parent-side aggregation plugin) must only see
    # replayed reports in the parent, not fork-inherited hooks in the child.
    for name in ("ztest-oracle",):
        plugin = pm.getplugin(name)
        if plugin is not None:
            pm.unregister(plugin)

    capman = pm.getplugin("capturemanager")
    if capman is not None:
        try:
            capman.stop_global_capturing()
            capman.start_global_capturing()
            capman.suspend_global_capture()
        except Exception:
            pass

    relay = WorkerReportRelay(conn, worker_id)
    relay._config = config
    pm.register(relay, f"ztest-worker-relay-{worker_id}")
    return relay


def run_worker(
    session: pytest.Session,
    items: list[pytest.Item],
    conn: Connection,
    worker_id: int,
) -> int:
    """Blocking worker loop: receive test ids, run them, stream reports.

    Protocol: the worker starts a test only when a following test is queued
    (it becomes ``nextitem``) or ``NO_MORE_TESTS`` has arrived (the last test
    runs with ``nextitem=None``). After each test it notifies the controller,
    which tops the queue back up, preserving one test of lookahead.
    """
    relay = prepare_forked_worker(session.config, conn, worker_id)

    queue: deque[int] = deque()
    no_more = False

    conn.send(MessageType.WORKER_READY, {"worker_id": worker_id, "pid": os.getpid()})

    def pump_until(condition) -> None:
        nonlocal no_more
        while not condition():
            try:
                frame = conn.recv()
            except ConnectionClosed:
                # Controller went away: nothing sane left to do.
                os._exit(3)
            if frame.type is MessageType.ASSIGN_TESTS:
                queue.extend(int(i) for i in frame.payload["test_ids"])
            elif frame.type is MessageType.NO_MORE_TESTS:
                no_more = True
            elif frame.type is MessageType.SHUTDOWN:
                no_more = True
                queue.clear()
            elif frame.type is MessageType.HEARTBEAT:
                conn.send(MessageType.HEARTBEAT, {"worker_id": worker_id})
            else:
                raise ProtocolError(f"unexpected message in worker: {frame.type}")

    while True:
        pump_until(lambda: queue or no_more)
        if not queue:
            break
        # Never run the last queued test until we know whether more work is
        # coming — its successor determines fixture teardown via nextitem.
        pump_until(lambda: len(queue) >= 2 or no_more)
        index = queue.popleft()
        item = items[index]
        nextitem = items[queue[0]] if queue else None
        relay.current_test_id = index
        session.config.hook.pytest_runtest_protocol(item=item, nextitem=nextitem)
        relay.current_test_id = None

    conn.send(MessageType.GOODBYE, {"worker_id": worker_id})
    return 0


def fork_worker(
    session: pytest.Session,
    items: list[pytest.Item],
    make_child_conn,
    worker_id: int,
) -> int:
    """Fork a worker from the current (post-collection) process.

    ``make_child_conn`` runs in the child and must return the child's framed
    Connection (prototype: pre-made socketpair end; host mode: a fresh
    connection to the controller socket). Returns the child pid in the parent.
    """
    pid = os.fork()
    if pid != 0:
        return pid
    # Child: never return into the parent's stack.
    status = 4
    try:
        conn = make_child_conn()
        status = run_worker(session, items, conn, worker_id)
        conn.close()
    except BaseException:
        try:
            import traceback

            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        finally:
            os._exit(5)
    finally:
        # Flush pending output, skip all parent atexit/teardown logic.
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(status)


def assert_fork_safe() -> list[str]:
    """Prefork safety checks (subset for M1): report reasons prefork is risky."""
    import threading

    problems: list[str] = []
    if threading.active_count() > 1:
        extra = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
        problems.append(f"unexpected active threads before fork: {extra}")
    try:
        import asyncio

        loop = asyncio.get_event_loop_policy()._local._loop  # type: ignore[attr-defined]
        if loop is not None and loop.is_running():
            problems.append("running asyncio event loop before fork")
    except Exception:
        pass
    if not hasattr(os, "fork"):
        problems.append("platform does not support fork")
    return problems


def now_ns() -> int:
    return time.monotonic_ns()
