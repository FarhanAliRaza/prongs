"""bolttest recorder plugin (PoC Phase 1, v4: one journal file per run).

Records, per test, the set of (file, qualname) project functions the test
entered, using sys.monitoring PY_START events. Non-project code objects
(stdlib, site-packages) are permanently DISABLEd on first sight and never
re-armed — we never call restart_events() — so framework internals cost
zero callbacks after the first test. Project functions stay armed and are
deduped into a per-test set (a set.add per project call).

Every session appends one journal file (see journal.py) and never touches
the map: `bolttest rollup` folds journal files into it. A run is full, partial
(node ids, -k, -m, --lf, an interrupted session, an xdist worker) or
collect-only. Partial runs are safe by construction — the roll-up refreshes
exactly the tests they observed and unions their import-time code into the
collection set — so the map can be fed from selected runs, and concurrent
sessions (worktrees, xdist workers) each write their own file.

Provenance: HEAD and every file that differed from it are snapshotted at
session start and again at finish, with content hashes, so the selector can
diff against the tree the evidence actually saw.

Opt-in: only active when --bolttest-cov is passed (or BOLTTEST_COV=1), so
installing the package does not perturb baseline runs. The journal directory
is --bolttest-journal, else $BOLTTEST_JOURNAL, else <state dir>/journal; the
file is named by --bolttest-journal-key (or $BOLTTEST_JOURNAL_KEY) when the
caller needs to find it again.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from bolttest import journal, provenance

MON = sys.monitoring
TOOL_ID = MON.COVERAGE_ID


def pytest_addoption(parser):
    parser.addoption(
        "--bolttest-cov",
        action="store_true",
        default=False,
        help="record per-test coverage and outcomes as a journal file (.bolttest/journal/)",
    )
    parser.addoption("--bolttest-journal", default=None, help="journal directory for --bolttest-cov")
    parser.addoption("--bolttest-journal-key", default=None, help="name of this run's journal file")


def _enabled(config) -> bool:
    ns = getattr(config, "known_args_namespace", None)  # all an early config has parsed
    return (
        bool(getattr(ns, "bolttest_cov", False))
        or bool(getattr(config.option, "bolttest_cov", False))
        or os.environ.get("BOLTTEST_COV") == "1"
    )


def _start(config) -> None:
    if _enabled(config) and not config.pluginmanager.has_plugin("bolttest-cov"):
        config.pluginmanager.register(BolttestCov(config), "bolttest-cov")


@pytest.hookimpl(wrapper=True)
def pytest_load_initial_conftests(early_config, parser, args):
    # start recording before any other plugin's start-up code runs:
    # pytest-django calls django.setup() in this hook and DRF-style conftests
    # in pytest_configure, and the models, settings and registries they
    # import are import-time code every test depends on
    _start(early_config)
    return (yield)


def pytest_configure(config):
    _start(config)  # plugins loaded too late for the early hook (-p, in-process)


def classify_scope(config) -> str:
    """'collect' for --collect-only; 'partial' when the invocation restricts
    the item set (node ids, files, subdirectories, -k, -m, --deselect, --lf);
    else 'full'. A session that ends with unobserved items is downgraded to
    'partial' at finish (see BolttestCov.pytest_sessionfinish)."""
    if config.getoption("collectonly", default=False):
        return "collect"
    opt = config.option
    if (
        getattr(opt, "keyword", None)
        or getattr(opt, "markexpr", None)
        or getattr(opt, "deselect", None)
        or getattr(opt, "lf", False)
        # xdist: a worker runs a subset, the controller runs nothing
        or hasattr(config, "workerinput")
        or getattr(opt, "dist", "no") not in (None, "no")
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


class BolttestCov:
    def __init__(self, config):
        self.config = config
        self.rootpath = str(config.rootpath) + os.sep
        # test_id -> (duration, status, frozenset[code])
        self.records: dict[str, tuple[float, str, frozenset]] = {}
        self._current: set = set()
        self._interesting: dict = {}  # code -> bool, classified once
        self._statuses: dict[str, str] = {}
        self.collect_errors: dict[str, str] = {}
        self.collect_skipped: list[str] = []
        self.n_collected = 0
        self.started_at = time.time()
        self.snapshot_start = provenance.snapshot(config.rootpath)
        MON.use_tool_id(TOOL_ID, "bolttest")
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

    def pytest_collectreport(self, report):
        if report.failed:
            lines = [ln for ln in str(report.longrepr).splitlines() if ln.strip()]
            self.collect_errors[report.nodeid] = lines[-1][:300] if lines else "collection error"
        elif report.skipped and report.nodeid:
            self.collect_skipped.append(report.nodeid)

    def pytest_collection_finish(self, session):
        # everything executed so far ran at start-up or import/collection time
        # (app setup, module bodies, decorators, class-level declarations): a
        # pseudo-context. A diff touching any of it invalidates the whole suite.
        self.n_collected = len(session.items)
        self.records[journal.COLLECTION] = (0.0, "collection", frozenset(self._current))

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
        n_observed = len(self.records) - (journal.COLLECTION in self.records)
        scope = classify_scope(self.config)  # at finish: the config is complete
        if scope == "full" and (n_observed < self.n_collected or self.n_collected == 0):
            scope = "partial"  # -x, interrupt, or nothing collected: no proof of completeness
        return {
            "started_at": self.started_at,
            "finished_at": time.time(),
            "scope": scope,
            "mode": "coverage",
            "worktree": self.rootpath.rstrip(os.sep),
            **provenance.between(self.snapshot_start, provenance.snapshot(Path(self.rootpath))),
            "recorder": provenance.recorder_fingerprint(),
            "args": list(self.config.invocation_params.args),
            "n_collected": self.n_collected,
            "collect_errors": self.collect_errors,
            "collect_skipped": self.collect_skipped,
        }

    def pytest_sessionfinish(self, session):
        MON.set_events(TOOL_ID, 0)
        MON.free_tool_id(TOOL_ID)
        t0 = time.monotonic()
        config = session.config
        jdir = config.getoption("bolttest_journal") or journal.journal_dir(config.rootpath)
        key = config.getoption("bolttest_journal_key") or os.environ.get("BOLTTEST_JOURNAL_KEY")
        info = self.run_info()
        res = journal.append(jdir, self.rootpath, self.records, info, key=key)
        sys.stderr.write(
            f"\n[bolttest] journaled run {res['key']} ({info['scope']}"
            f"{', dirty' if info['dirty_files'] else ''}): {res['n_tests']} tests"
            f" -> {res['n_funcs']} funcs ({time.monotonic() - t0:.2f}s) -> {res['path']}\n"
        )
