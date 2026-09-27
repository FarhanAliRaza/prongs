"""Phase 3: warm daemon + fork-per-run.

Parent boots once: runs `pytest --collect-only` in-process, which imports the
entire test tree (with assertion rewriting) and triggers app setup
(e.g. django.setup() via pytest-django's plugin hooks). Then it listens on a
Unix socket; each request forks, and the CHILD runs real pytest.main() with
the requested node IDs — full fixture/plugin compat — streaming a JSON result
back over the connection, then os._exit()s. Copy-on-write makes each run
start from pristine warm state.

Server: python -m bolttest.daemon serve  (run from the repo root)
Client: python -m bolttest.daemon run tests/test_x.py::test_y ...
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import sys
import time

SOCK = ".bolttest/daemon.sock"


def send_msg(conn: socket.socket, obj) -> None:
    data = json.dumps(obj).encode()
    conn.sendall(struct.pack(">I", len(data)) + data)


def recv_msg(conn: socket.socket):
    hdr = b""
    while len(hdr) < 4:
        chunk = conn.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    (n,) = struct.unpack(">I", hdr)
    data = b""
    while len(data) < n:
        chunk = conn.recv(min(65536, n - len(data)))
        if not chunk:
            return None
        data += chunk
    return json.loads(data)


# pytest exit codes that mean the session did not do what was asked: the
# result list is not a verdict on the targets, whatever it contains
ERROR_EXITS = {
    2: "interrupted (collection errors)",
    3: "internal error",
    4: "usage error (bad arguments or node ids not found)",
    5: "no tests collected",
}


def execution_response(code, wall_s: float, collector: "ResultCollector", output) -> dict:
    """The executor's reply, same schema from the daemon child and the cold
    subprocess. `output` is a callable returning pytest's captured text, read
    only when the exit code means the run went wrong."""
    resp = {
        "exit": int(code),
        "wall_s": round(wall_s, 4),
        "results": collector.results,
        "collect_errors": collector.collect_errors,
        "collect_skipped": collector.collect_skipped,
    }
    if int(code) in ERROR_EXITS:
        resp["error"] = f"pytest exit {int(code)}: {ERROR_EXITS[int(code)]}"
    if int(code) not in (0, 1):
        resp["pytest_output"] = output()[-3000:]
    return resp


def crash_line(longrepr) -> str | None:
    """First line of pytest's crash message (what its short summary prints
    per failure), when the report carries one."""
    crash = getattr(longrepr, "reprcrash", None)
    message = getattr(crash, "message", None)
    return message.strip().splitlines()[0] if message and message.strip() else None


class ResultCollector:
    """pytest plugin: per-test results, one entry per test id. Setup errors,
    subtest reports (pytest >= 9) and teardown errors are merged into their
    test's entry, failure winning, so the entry count is the number of tests
    that produced a result. Collection errors and module-level skips are
    kept apart: no test id carries them."""

    def __init__(self):
        self.results: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self.collect_errors: list[dict] = []
        self.collect_skipped: list[str] = []

    def pytest_runtest_logreport(self, report):
        if report.passed and report.when != "call":
            return  # setup/teardown that passed says nothing about the test
        entry = self._by_id.get(report.nodeid)
        if entry is None:
            entry = self._by_id[report.nodeid] = {
                "id": report.nodeid,
                "status": report.outcome,
                "duration_s": round(report.duration, 4),
            }
            self.results.append(entry)
        elif report.when == "call" and entry["status"] != "failed":
            entry["status"] = report.outcome
            entry["duration_s"] = round(report.duration, 4)
        if report.failed and "longrepr" not in entry:
            entry["status"] = "failed"
            entry["longrepr"] = str(report.longrepr)[-4000:]
            crash = crash_line(report.longrepr)
            if crash:
                entry["crash"] = crash[:500]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.append(
                {"id": report.nodeid, "longrepr": str(report.longrepr)[-2000:]}
            )
        elif report.skipped and report.nodeid:
            self.collect_skipped.append(report.nodeid)


class _Quiet:
    """Route fd 1 to /dev/null for the duration (pytest writes to fd-level stdout)."""

    def __enter__(self):
        sys.stdout.flush()
        self._saved = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)

    def __exit__(self, *exc):
        sys.stdout.flush()
        os.dup2(self._saved, 1)
        os.close(self._saved)


def warm() -> float:
    """Import the world once in the parent. Returns warm-up seconds."""
    import shlex

    import pytest

    t0 = time.monotonic()
    extra = shlex.split(os.environ.get("BOLTTEST_WARM_ARGS", ""))
    # --co imports every test module (assertion-rewritten) and runs plugin
    # configure hooks (django.setup() etc.) without executing any test.
    with _Quiet():
        pytest.main(
            ["--collect-only", "-q", "--no-header", "-p", "no:cacheprovider"] + extra,
            plugins=[],
        )
    # fixture snapshot: run one DB-touching test in the parent so the test
    # database (schema + fixtures) lives in the warm image; every forked
    # child then inherits a pristine copy-on-write copy of it.
    db_test = os.environ.get("BOLTTEST_WARM_DB_TEST")
    if db_test:
        run_args = shlex.split(os.environ.get("BOLTTEST_RUN_ARGS", ""))
        with _Quiet():
            pytest.main(
                [db_test, "-q", "--no-header", "-p", "no:cacheprovider"] + run_args,
                plugins=[],
            )
        # keep the test DB alive in the warm image: for in-memory SQLite the
        # database exists only while a connection holds it, so pin one open.
        # Every fork then inherits a pristine CoW copy of the populated DB.
        try:
            from django.db import connections

            for alias in connections:
                connections[alias].connect()
        except Exception:
            pass
    return time.monotonic() - t0


def skip_db_setup_in_child() -> None:
    """The forked image already contains the populated test DB (created once
    in the warm parent, kept alive by a pinned connection). Make Django's
    setup_databases a no-op so pytest-django reuses it instead of rebuilding.
    """
    def fake_setup_databases(*args, **kwargs):
        return []

    try:
        import django.test.utils as dtu

        dtu.setup_databases = fake_setup_databases
        import pytest_django.fixtures as pdf

        if hasattr(pdf, "setup_databases"):
            pdf.setup_databases = fake_setup_databases
    except ImportError:
        pass


def apply_compat_shims() -> None:
    """The 'post-fork re-init hook list' from the hypothesis, PoC edition.

    django: conftests commonly call settings.configure() unconditionally in
    pytest_configure; the warm parent already configured, so make the second
    call (in the forked child's pytest.main) a no-op instead of a crash.
    """
    try:
        from django.conf import LazySettings
    except ImportError:
        return
    orig = LazySettings.configure

    def configure_once(self, *args, **kwargs):
        if self.configured:
            return
        orig(self, *args, **kwargs)

    LazySettings.configure = configure_once

    # allow project model classes to re-register after a module purge
    try:
        from django.apps.registry import Apps
    except ImportError:
        return
    orig_rm = Apps.register_model

    def register_model(self, app_label, model):
        self.all_models[app_label].pop(model._meta.model_name, None)
        orig_rm(self, app_label, model)
        self.clear_cache()

    Apps.register_model = register_model


def purge_stale_project_modules(rootpath: str, warm_time: float) -> int:
    """If any imported project file changed since warm-up, drop ALL project
    modules from sys.modules so the child's pytest re-imports them fresh.
    Third-party/site-packages modules (the import weight) stay warm.
    Purging the whole project closure sidesteps stale from-import references.
    """
    project: dict[str, str] = {}
    stale = False
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if f and f.startswith(rootpath) and "site-packages" not in f:
            project[name] = f
            try:
                if os.stat(f).st_mtime > warm_time:
                    stale = True
            except OSError:
                stale = True
    if not stale:
        return 0
    # sys.modules preserves first-import order; replaying it re-resolves
    # circular imports the same way the warm-up did. Test modules stay
    # purged so pytest re-imports them via its assertion-rewriting loader.
    names = [n for n in sys.modules if n in project]
    for name in names:
        del sys.modules[name]
    try:
        from django.apps import apps

        apps.clear_cache()
        from django.urls import clear_url_caches

        clear_url_caches()
    except ImportError:
        pass
    import importlib

    errors = []
    for name in names:
        f = project[name]
        base = os.path.basename(f)
        if base.startswith("test_") or base == "conftest.py" or "/tests/" in f:
            continue
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e!r}")
    if errors:
        # the warm image can't be safely reused (import-order cycles,
        # third-party registries like taggit). Refuse rather than run wrong.
        raise StaleWarmImage(errors)
    return len(names)


class StaleWarmImage(Exception):
    """Warm image is stale and re-import failed; caller must run cold."""

    def __init__(self, errors):
        super().__init__(f"{len(errors)} project modules failed re-import")
        self.errors = errors


def serve() -> None:
    os.makedirs(".bolttest", exist_ok=True)
    rootpath = os.getcwd() + os.sep
    apply_compat_shims()
    dt = warm()
    warm_time = time.time()
    sys.stderr.write(f"[bolttest-daemon] warm in {dt:.2f}s, listening on {SOCK}\n")

    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(8)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # reap children automatically

    while True:
        conn, _ = srv.accept()
        req = recv_msg(conn)
        if req is None:
            conn.close()
            continue
        if req.get("op") == "stop":
            send_msg(conn, {"ok": True})
            conn.close()
            break
        pid = os.fork()
        if pid == 0:  # child
            srv.close()
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)  # pytest may spawn subprocesses
            t0 = time.monotonic()
            try:
                try:
                    n_purged = purge_stale_project_modules(rootpath, warm_time)
                except StaleWarmImage as e:
                    send_msg(conn, {"stale": True, "errors": e.errors[:5]})
                    conn.close()
                    os._exit(0)
                import pytest

                import shlex

                collector = ResultCollector()
                args = (
                    shlex.split(os.environ.get("BOLTTEST_RUN_ARGS", ""))
                    + req.get("args", [])
                    + req["node_ids"]
                    + ["-q", "--no-header", "-p", "no:cacheprovider"]
                )
                if os.environ.get("BOLTTEST_WARM_DB_TEST"):
                    skip_db_setup_in_child()
                import tempfile

                buf = tempfile.TemporaryFile()
                sys.stdout.flush()
                sys.stderr.flush()
                saved1, saved2 = os.dup(1), os.dup(2)
                os.dup2(buf.fileno(), 1)
                os.dup2(buf.fileno(), 2)
                try:
                    code = pytest.main(args, plugins=[collector])
                finally:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os.dup2(saved1, 1)
                    os.dup2(saved2, 2)
                def output() -> str:
                    buf.seek(0)
                    return buf.read().decode(errors="replace")

                resp = execution_response(code, time.monotonic() - t0, collector, output)
                resp["purged_modules"] = n_purged
                send_msg(conn, resp)
            except BaseException as e:  # noqa: BLE001
                try:
                    send_msg(conn, {"error": repr(e)})
                except Exception:
                    pass
            finally:
                conn.close()
                os._exit(0)
        conn.close()  # parent
    srv.close()
    os.unlink(SOCK)


def run(node_ids: list[str]) -> None:
    t0 = time.monotonic()
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    for _ in range(100):  # daemon may still be warming up
        try:
            conn.connect(SOCK)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.1)
    send_msg(conn, {"op": "run", "node_ids": node_ids})
    resp = recv_msg(conn)
    conn.close()
    wall = time.monotonic() - t0
    resp["client_wall_s"] = round(wall, 4)
    print(json.dumps(resp, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve()
    elif sys.argv[1] == "run":
        run(sys.argv[2:])
    elif sys.argv[1] == "stop":
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(SOCK)
        send_msg(c, {"op": "stop"})
        recv_msg(c)
        c.close()
