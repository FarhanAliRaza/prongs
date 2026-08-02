"""Milestone 1: the pure-Python collect-once prefork controller.

This is the decisive experiment. Normal pytest starts, loads configuration
and plugins, collects every test exactly once — then this controller forks N
warm workers from the collected state and schedules test indexes dynamically
over socketpairs. Workers execute through the real pytest runtest protocol
and stream serialized phase reports back; the parent replays them through the
standard logstart/logreport/logfinish hooks so the terminal reporter, exit
status and any reporting plugin behave as in a serial run.
"""

from __future__ import annotations

import os
import selectors
import signal
import socket
import sys
from collections import deque
from dataclasses import dataclass, field

import pytest

from .protocol import Connection, Frame, MessageType, ProtocolError
from .reporting import deserialize_report
from .worker import assert_fork_safe, fork_worker


@dataclass
class WorkerState:
    worker_id: int
    pid: int
    conn: Connection
    outstanding: set[int] = field(default_factory=set)
    sent_no_more: bool = False
    ready: bool = False
    done: bool = False


class PreforkController:
    def __init__(
        self,
        session: pytest.Session,
        jobs: int,
        *,
        initial_batch: int = 2,
        max_crash_requeues: int = 1,
    ) -> None:
        self.session = session
        self.config = session.config
        self.items = session.items
        self.jobs = max(1, min(jobs, len(self.items) or 1))
        self.initial_batch = max(2, initial_batch)
        self.max_crash_requeues = max_crash_requeues
        self.pending: deque[int] = deque(range(len(self.items)))
        self.finished: set[int] = set()
        self.requeue_counts: dict[int, int] = {}
        self.workers: dict[int, WorkerState] = {}
        self.selector = selectors.DefaultSelector()
        self.protocol_errors: list[str] = []

    # -- worker lifecycle ---------------------------------------------------

    def spawn_worker(self, worker_id: int) -> WorkerState:
        parent_sock, child_sock = socket.socketpair()

        def make_child_conn() -> Connection:
            parent_sock.close()
            return Connection(child_sock)

        pid = fork_worker(self.session, self.items, make_child_conn, worker_id)
        child_sock.close()
        conn = Connection(parent_sock)
        state = WorkerState(worker_id=worker_id, pid=pid, conn=conn)
        self.workers[worker_id] = state
        self.selector.register(parent_sock, selectors.EVENT_READ, state)
        return state

    def _assign(self, state: WorkerState, count: int) -> None:
        ids = []
        while self.pending and len(ids) < count:
            ids.append(self.pending.popleft())
        if ids:
            state.outstanding.update(ids)
            state.conn.send(MessageType.ASSIGN_TESTS, {"test_ids": ids})
        if not self.pending and not state.sent_no_more:
            state.sent_no_more = True
            state.conn.send(MessageType.NO_MORE_TESTS, {})

    # -- report replay ------------------------------------------------------

    def _replay(self, frame: Frame) -> None:
        payload = frame.payload
        hook = self.config.hook
        if frame.type is MessageType.TEST_STARTED:
            hook.pytest_runtest_logstart(
                nodeid=payload["nodeid"], location=tuple(payload["location"])
            )
        elif frame.type is MessageType.TEST_REPORT:
            report = deserialize_report(self.config, payload["serialized_report"])
            hook.pytest_runtest_logreport(report=report)
        elif frame.type is MessageType.TEST_FINISHED:
            hook.pytest_runtest_logfinish(
                nodeid=payload["nodeid"], location=tuple(payload["location"])
            )

    def _synthesize_crash_report(self, test_index: int, reason: str) -> None:
        item = self.items[test_index]
        try:
            report = pytest.TestReport(
                nodeid=item.nodeid,
                location=item.location,
                keywords={},
                outcome="failed",
                longrepr=reason,
                when="call",
                sections=[],
                duration=0.0,
                start=0.0,
                stop=0.0,
            )
        except TypeError:
            report = pytest.TestReport(  # older signature without start/stop
                nodeid=item.nodeid,
                location=item.location,
                keywords={},
                outcome="failed",
                longrepr=reason,
                when="call",
                sections=[],
                duration=0.0,
            )
        self.config.hook.pytest_runtest_logstart(
            nodeid=item.nodeid, location=item.location
        )
        self.config.hook.pytest_runtest_logreport(report=report)
        self.config.hook.pytest_runtest_logfinish(
            nodeid=item.nodeid, location=item.location
        )
        self.finished.add(test_index)

    # -- event handling -----------------------------------------------------

    def _handle_frame(self, state: WorkerState, frame: Frame) -> None:
        if frame.type is MessageType.WORKER_READY:
            state.ready = True
            self._assign(state, self.initial_batch)
        elif frame.type in (
            MessageType.TEST_STARTED,
            MessageType.TEST_REPORT,
        ):
            self._replay(frame)
        elif frame.type is MessageType.TEST_FINISHED:
            self._replay(frame)
            test_id = frame.payload.get("test_id")
            if test_id is not None:
                if test_id in self.finished:
                    self.protocol_errors.append(
                        f"duplicate completion for test {test_id}"
                    )
                self.finished.add(test_id)
                state.outstanding.discard(test_id)
            self._assign(state, 1)
        elif frame.type is MessageType.GOODBYE:
            state.done = True
            self._retire(state)
        elif frame.type is MessageType.HEARTBEAT:
            pass
        else:
            raise ProtocolError(f"unexpected message from worker: {frame.type}")

    def _retire(self, state: WorkerState) -> None:
        try:
            self.selector.unregister(state.conn._sock)
        except (KeyError, ValueError):
            pass
        state.conn.close()
        try:
            os.waitpid(state.pid, 0)
        except ChildProcessError:
            pass
        del self.workers[state.worker_id]

    def _handle_worker_death(self, state: WorkerState, reason: str) -> None:
        """A worker vanished mid-run. Recover its outstanding tests."""
        lost = sorted(state.outstanding)
        state.outstanding.clear()
        state.done = True
        self._retire(state)
        for test_index in lost:
            requeues = self.requeue_counts.get(test_index, 0)
            if requeues < self.max_crash_requeues:
                self.requeue_counts[test_index] = requeues + 1
                self.pending.appendleft(test_index)
            else:
                self._synthesize_crash_report(
                    test_index,
                    f"worker {state.worker_id} died while this test was "
                    f"assigned ({reason}); attempt limit reached",
                )
        # Replace the worker if there is still work to do.
        if self.pending:
            new_id = max(self.workers.keys(), default=state.worker_id) + 1
            self.spawn_worker(new_id)

    # -- main loop ----------------------------------------------------------

    def run(self) -> None:
        capman = self.config.pluginmanager.getplugin("capturemanager")
        if capman is not None:
            try:
                capman.suspend_global_capture(in_=False)
            except Exception:
                pass

        for worker_id in range(self.jobs):
            self.spawn_worker(worker_id)

        try:
            while self.workers:
                events = self.selector.select(timeout=30.0)
                if not events:
                    # Nothing readable for a long time: check for dead children.
                    for state in list(self.workers.values()):
                        pid, status = os.waitpid(state.pid, os.WNOHANG)
                        if pid != 0:
                            self._handle_worker_death(
                                state, f"exit status {status}"
                            )
                    continue
                for key, _mask in events:
                    state: WorkerState = key.data
                    if state.done:
                        continue
                    try:
                        frames, eof = state.conn.drain()
                    except ProtocolError as exc:
                        self.protocol_errors.append(str(exc))
                        self._handle_worker_death(state, f"protocol error: {exc}")
                        continue
                    for frame in frames:
                        self._handle_frame(state, frame)
                        if state.done:
                            break
                    if eof and not state.done:
                        self._handle_worker_death(state, "unexpected EOF")
        except KeyboardInterrupt:
            for state in list(self.workers.values()):
                try:
                    os.kill(state.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            raise
        finally:
            for state in list(self.workers.values()):
                self._retire(state)

        missing = set(range(len(self.items))) - self.finished
        if missing:
            for test_index in sorted(missing):
                self._synthesize_crash_report(
                    test_index, "test was never executed (scheduler accounting bug)"
                )
            self.protocol_errors.append(f"{len(missing)} tests never ran")
        if self.protocol_errors:
            print(
                "ztest-prototype internal errors: " + "; ".join(self.protocol_errors),
                file=sys.stderr,
            )


def run_prefork(session: pytest.Session, jobs: int, *, initial_batch: int = 2) -> None:
    problems = assert_fork_safe()
    if problems:
        raise pytest.UsageError(
            "prefork engine refused to start: " + "; ".join(problems)
        )
    PreforkController(session, jobs, initial_batch=initial_batch).run()
