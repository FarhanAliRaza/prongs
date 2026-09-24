"""fastest recorder plugin (PoC Phase 1, v3: journal + provenance).

Records, per test, the set of (file, qualname) project functions the test
entered, using sys.monitoring PY_START events. Non-project code objects
(stdlib, site-packages) are permanently DISABLEd on first sight and never
re-armed — we never call restart_events() — so framework internals cost
zero callbacks after the first test. Project functions stay armed and are
deduped into a per-test set (a set.add per project call).

Every session is appended to the journal as one run (see mapdb): a full
run, a partial run (node ids, -k, -m, --lf, an interrupted session, an xdist
worker) or a collect-only run. Partial runs are safe by construction — they
refresh exactly the tests they observed and union their import-time code
into the collection set — so the map can be fed from selected runs.

Provenance: HEAD and every file that differed from it are snapshotted at
session start and again at finish, with content hashes, so the selector can
diff against the tree the evidence actually saw.

Opt-in: only active when --fastest-cov is passed (or FASTEST_COV=1), so
installing the package does not perturb baseline runs.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from fastest import provenance

MON = sys.monitoring
TOOL_ID = MON.COVERAGE_ID


def pytest_addoption(parser):
    parser.addoption(
        "--fastest-cov",
        action="store_true",
        default=False,
        help="record per-test coverage into the .fastest/map.sqlite journal",
    )


def pytest_configure(config):
    if config.getoption("--fastest-cov") or os.environ.get("FASTEST_COV") == "1":
        config.pluginmanager.register(FastestCov(config), "fastest-cov")


def classify_scope(config) -> str:
    """'collect' for --collect-only; 'partial' when the invocation restricts
    the item set (node ids, files, subdirectories, -k, -m, --deselect, --lf);
    else 'full'. A session that ends with unobserved items is downgraded to
    'partial' at finish (see FastestCov.pytest_sessionfinish)."""
    if config.getoption("collectonly", default=False):
        return "collect"
    opt = config.option
    if (
        getattr(opt, "keyword", None)
        or getattr(opt, "markexpr", None)
        or getattr(opt, "deselect", None)
        or getattr(opt, "lf", False)
    ):
        return "partial"
    source = getattr(config, "args_source", None)
    if source is not None and source != config.ArgsSource.ARGS:
        return "full"  # no positional args: pytest chose testpaths / cwd
    targets = [config.rootpath / p for p in (config.getini("testpaths") or ["."])]
    invocation_dir = Path(config.invocation_params.dir)
    covered = []
    for arg in config.args:
        if "::" in arg:
            return "partial"
        p = Path(arg)
        p = (p if p.is_absolute() else invocation_dir / p).resolve()
        if p.is_file():
            return "partial"
        covered.append(p)
    for t in targets:
        t = t.resolve()
        if not any(t == c or c in t.parents for c in covered):
            return "partial"
    return "full"


class FastestCov:
    def __init__(self, config):
        self.config = config
        self.rootpath = str(config.rootpath) + os.sep
        # test_id -> (duration, status, frozenset[code])
        self.records: dict[str, tuple[float, str, frozenset]] = {}
        self._current: set = set()
        self._interesting: dict = {}  # code -> bool, classified once
        self._statuses: dict[str, str] = {}
        self.n_collected = 0
        self.started_at = time.time()
        self.scope = classify_scope(config)
        self.snapshot_start = provenance.snapshot(config.rootpath)
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
        self.n_collected = len(session.items)
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

    def run_info(self) -> dict:
        """Provenance for this session, taken at finish."""
        end = provenance.snapshot(Path(self.rootpath))
        start = self.snapshot_start
        dirty = {
            p: [start["dirty"].get(p), end["dirty"].get(p)]
            for p in start["dirty"].keys() | end["dirty"].keys()
        }
        n_observed = len(self.records) - ("__collection__" in self.records)
        scope = self.scope
        if scope == "full" and n_observed < self.n_collected:
            scope = "partial"  # -x, interrupt, xdist worker: not every item ran
        return {
            "started_at": self.started_at,
            "finished_at": time.time(),
            "scope": scope,
            "commit": start["commit"],
            "dirty_files": dirty,
            "blobs": end["contents"],  # what the run actually saw of dirty files
            "tree_changed": start["commit"] != end["commit"] or start["dirty"] != end["dirty"],
            "recorder": provenance.recorder_fingerprint(),
            "args": list(self.config.invocation_params.args),
            "n_collected": self.n_collected,
        }

    def pytest_sessionfinish(self, session):
        MON.set_events(TOOL_ID, 0)
        MON.free_tool_id(TOOL_ID)
        t0 = time.monotonic()
        from fastest.mapdb import record_run

        path = os.environ.get("FASTEST_DB") or os.path.join(
            str(session.config.rootpath), ".fastest", "map.sqlite"
        )
        info = self.run_info()
        res = record_run(path, self.rootpath, self.records, info)
        sys.stderr.write(
            f"\n[fastest] run #{res['run_id']} ({info['scope']}"
            f"{', dirty' if info['dirty_files'] else ''}): {res['n_tests']} tests"
            f" -> {res['n_funcs']} funcs, {res['n_new_sets']} new dep sets"
            f" ({time.monotonic() - t0:.2f}s) -> {path}\n"
        )
