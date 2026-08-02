"""fastest coverage-map plugin (PoC Phase 1, v2).

Records, per test, the set of (file, qualname) project functions the test
entered, using sys.monitoring PY_START events. Non-project code objects
(stdlib, site-packages) are permanently DISABLEd on first sight and never
re-armed — we never call restart_events() — so framework internals cost
zero callbacks after the first test. Project functions stay armed and are
deduped into a per-test set (a set.add per project call).

Opt-in: only active when --fastest-cov is passed (or FASTEST_COV=1), so
installing the package does not perturb baseline runs.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

MON = sys.monitoring
TOOL_ID = MON.COVERAGE_ID


def pytest_addoption(parser):
    parser.addoption(
        "--fastest-cov",
        action="store_true",
        default=False,
        help="record per-test coverage map into .fastest/map.sqlite",
    )


def pytest_configure(config):
    if config.getoption("--fastest-cov") or os.environ.get("FASTEST_COV") == "1":
        config.pluginmanager.register(FastestCov(config), "fastest-cov")


class FastestCov:
    def __init__(self, config):
        self.rootpath = str(config.rootpath) + os.sep
        # test_id -> (duration, status, frozenset[code])
        self.records: dict[str, tuple[float, str, frozenset]] = {}
        self._current: set = set()
        self._interesting: dict = {}  # code -> bool, classified once
        self._statuses: dict[str, str] = {}
        MON.use_tool_id(TOOL_ID, "fastest")
        MON.register_callback(TOOL_ID, MON.events.PY_START, self._on_start)
        MON.set_events(TOOL_ID, MON.events.PY_START)

    def _on_start(self, code, offset):
        interesting = self._interesting.get(code)
        if interesting is None:
            fn = code.co_filename
            interesting = self._interesting[code] = (
                fn.startswith(self.rootpath)
                and "site-packages" not in fn
                and not fn.startswith("<")
            )
        if not interesting:
            return MON.DISABLE
        self._current.add(code)

    def pytest_collection_finish(self, session):
        # everything executed so far ran at import/collection time (module
        # bodies, decorators, class-level declarations): a pseudo-context.
        # A diff touching any of it invalidates the whole suite.
        self.records["__collection__"] = (0.0, "collection", frozenset(self._current))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self._current = set()
        t0 = time.monotonic()
        try:
            return (yield)
        finally:
            dur = time.monotonic() - t0
            status = self._statuses.pop(item.nodeid, "unknown")
            self.records[item.nodeid] = (dur, status, frozenset(self._current))

    def pytest_runtest_logreport(self, report):
        # remember the worst outcome across setup/call/teardown
        if report.failed:
            self._statuses[report.nodeid] = "failed"
        elif report.skipped:
            self._statuses.setdefault(report.nodeid, "skipped")
        elif report.when == "call":
            self._statuses.setdefault(report.nodeid, "passed")

    def pytest_sessionfinish(self, session):
        MON.set_events(TOOL_ID, 0)
        MON.free_tool_id(TOOL_ID)
        t0 = time.monotonic()
        from fastest.mapdb import dump

        path = os.environ.get("FASTEST_DB") or os.path.join(
            str(session.config.rootpath), ".fastest", "map.sqlite"
        )
        n_tests, n_funcs = dump(path, self.rootpath, self.records)
        sys.stderr.write(
            f"\n[fastest] map: {n_tests} tests -> {n_funcs} funcs "
            f"({time.monotonic() - t0:.2f}s dump) -> {path}\n"
        )
