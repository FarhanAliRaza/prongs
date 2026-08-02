"""Milestone 3: the Python pytest host driven by the Zig controller.

Started by the Zig binary as:

    python -m ztest_py host --socket /path/to/ztest.sock -- <pytest args>

The host runs normal pytest startup — configuration, plugins, assertion
rewriting, conftest loading — collects every test exactly once, sends the
manifest to the controller over the control socket, then forks workers on
demand. Each forked worker opens its *own* connection to the controller and
executes assigned test indexes via the real pytest runtest protocol; the
host never runs tests itself.

Connection handshake convention: the host's first frame is HELLO with
role=host; a worker's first frame is WORKER_READY carrying its worker_id.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest

from .manifest import build_manifest
from .protocol import Connection, ConnectionClosed, MessageType
from .worker import assert_fork_safe, fork_worker


class HostPlugin:
    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self.items: list[pytest.Item] = []
        self.child_pids: list[int] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.items = list(session.items)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool | None:
        if session.config.option.collectonly:
            return None

        problems = assert_fork_safe()
        if problems:
            raise pytest.UsageError(
                "prefork engine refused to start: " + "; ".join(problems)
            )

        # Rendering belongs to the Zig controller; drop the host's terminal
        # reporter so it doesn't print a misleading "no tests ran" summary.
        pm = session.config.pluginmanager
        terminal = pm.getplugin("terminalreporter")
        if terminal is not None:
            pm.unregister(terminal)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        conn = Connection(sock)

        conn.send(
            MessageType.HELLO,
            {"role": "host", "pid": os.getpid(), "python": sys.version.split()[0]},
        )
        manifest = build_manifest(self.items)
        conn.send(MessageType.MANIFEST_BEGIN, {"count": len(manifest)})
        for entry in manifest:
            conn.send(MessageType.MANIFEST_ITEM, entry)
        conn.send(MessageType.MANIFEST_END, {})
        conn.send(MessageType.HOST_READY, {})

        sock.settimeout(1.0)
        try:
            while True:
                self._reap_children()
                try:
                    frame = conn.recv()
                except socket.timeout:
                    continue
                except ConnectionClosed:
                    break  # Controller went away; shut down.
                if frame.type is MessageType.SPAWN_WORKER:
                    worker_id = int(frame.payload["worker_id"])
                    pid = self._spawn_worker(session, worker_id)
                    self.child_pids.append(pid)
                elif frame.type is MessageType.SHUTDOWN:
                    break
                elif frame.type is MessageType.HEARTBEAT:
                    conn.send(MessageType.HEARTBEAT, {})
        finally:
            self._shutdown_children()
            conn.close()
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass
        return True

    def _spawn_worker(self, session: pytest.Session, worker_id: int) -> int:
        socket_path = self.socket_path

        def make_child_conn() -> Connection:
            child_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            child_sock.connect(socket_path)
            return Connection(child_sock)

        return fork_worker(session, self.items, make_child_conn, worker_id)

    def _reap_children(self) -> None:
        alive = []
        for pid in self.child_pids:
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done == 0:
                alive.append(pid)
        self.child_pids = alive

    def _shutdown_children(self) -> None:
        import signal
        import time

        deadline = time.monotonic() + 5.0
        while self.child_pids and time.monotonic() < deadline:
            self._reap_children()
            time.sleep(0.05)
        for pid in self.child_pids:
            try:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass


