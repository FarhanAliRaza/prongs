"""prongs recorder plugin (PoC Phase 1, v4: one journal file per run).

Records, per test, the set of (file, qualname) project functions the test
entered, using sys.monitoring PY_START events. Non-project code objects
(stdlib, site-packages) are permanently DISABLEd on first sight and never
re-armed — we never call restart_events() — so framework internals cost
zero callbacks after the first test. Project functions stay armed and are
deduped into a per-test set (a set.add per project call).

Every session appends one journal file (see journal.py) and never touches
the map: `prongs rollup` folds journal files into it. A run is full, partial
(node ids, -k, -m, --lf, an interrupted session, an xdist worker) or
collect-only. Partial runs are safe by construction — the roll-up refreshes
exactly the tests they observed and unions their import-time code into the
collection set — so the map can be fed from selected runs, and concurrent
sessions (worktrees, xdist workers) each write their own file.

Provenance: HEAD and every file that differed from it are snapshotted at
session start and again at finish, with content hashes, so the selector can
diff against the tree the evidence actually saw.

Opt-in: only active when --prongs-cov is passed (or PRONGS_COV=1), so
installing the package does not perturb baseline runs. The journal directory
is --prongs-journal, else $PRONGS_JOURNAL, else <state dir>/journal; the
file is named by --prongs-journal-key (or $PRONGS_JOURNAL_KEY) when the
caller needs to find it again.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from prongs import config as settings
from prongs import evidence, journal, provenance, state

# sys.monitoring (3.12+) costs nothing for code it has disabled; before it,
# a profile function sees every call, which is slower but records the same
MON = getattr(sys, "monitoring", None)
TOOL_ID = MON.COVERAGE_ID if MON else None


def pytest_addoption(parser):
    parser.addoption(
        "--prongs-cov",
        action="store_true",
        default=False,
        help="record per-test coverage and outcomes as a journal file (.prongs/journal/)",
    )
    parser.addoption("--prongs-journal", default=None, help="journal directory for --prongs-cov")
    parser.addoption("--prongs-journal-key", default=None, help="name of this run's journal file")


def _enabled(config) -> bool:
    ns = getattr(config, "known_args_namespace", None)  # all an early config has parsed
    return (
        bool(getattr(ns, "prongs_cov", False))
        or bool(getattr(config.option, "prongs_cov", False))
        or os.environ.get("PRONGS_COV") == "1"
    )


def _start(config) -> None:
    if _enabled(config) and not config.pluginmanager.has_plugin("prongs-cov"):
        config.pluginmanager.register(ProngsCov(config), "prongs-cov")


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


def pytest_collection_finish(session):
    # a session asked for what import produced (state.collect): any session,
    # recording or not
    out = os.environ.get(state.OUT)
    if out:
        with open(out, "w") as f:
            json.dump(state.snapshot(str(session.config.rootpath) + os.sep), f)


def classify_scope(config) -> str:
    """'collect' for --collect-only; 'partial' when the invocation restricts
    the item set (node ids, files, subdirectories, -k, -m, --deselect, --lf);
    else 'full'. A session that ends with unobserved items is downgraded to
    'partial' at finish (see ProngsCov.pytest_sessionfinish)."""
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


class ProngsCov:
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
        # what function entry cannot see: files read, names listed, processes
        # started (evidence.py). Shared fixtures are set up once, inside the
        # first test that asks for them: what their setup did is kept per
        # fixture and credited to every test that uses it.
        self.evidence = evidence.Evidence(
            self.rootpath, provenance.tracked_files(config.rootpath),
            settings.names(config.rootpath, "native_commands"),
        )
        self._fixtures: dict = {}  # FixtureDef -> (deps, started a process)
        self._shared: tuple[set, bool] = (set(), False)
        self._spawns: dict[str, tuple[int, bool]] = {}  # test -> (own spawns, via a fixture)
        # read now: the recorder's own look at pyproject.toml is not the suite's
        self._want_state = settings.import_state(config.rootpath) is not None
        self._modules: list[str] = []
        self.evidence.start()
        if MON:
            MON.use_tool_id(TOOL_ID, "prongs")
            MON.register_callback(TOOL_ID, MON.events.PY_START, self._on_start)
            MON.set_events(TOOL_ID, MON.events.PY_START)
        else:
            import threading

            sys.setprofile(self._profile)
            threading.setprofile(self._profile)

    def _interest(self, code) -> bool:
        fn = code.co_filename
        interesting = self._interesting[code] = (
            fn.startswith(self.rootpath)
            and "site-packages" not in fn
            and not fn.startswith("<")
        )
        return interesting

    def _on_start(self, code, offset):
        interesting = self._interesting.get(code)
        if interesting is None:
            interesting = self._interest(code)
        if not interesting:
            return MON.DISABLE
        self._current.add(code)

    def _profile(self, frame, event, arg):
        if event == "call":
            code = frame.f_code
            interesting = self._interesting.get(code)
            if interesting is None:
                interesting = self._interest(code)
            if interesting:
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
        self._modules = sorted({item.nodeid.split("::", 1)[0] for item in session.items})
        # a process started while collecting is not marked: what collection
        # runs is already a reason to run everything when it changes
        self.records[journal.COLLECTION] = (
            0.0, "collection", frozenset(self._current | self.evidence.deps))

    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef, request):
        if fixturedef.scope == "function":
            return (yield)
        ev = self.evidence
        outer = self._current, ev.deps, ev.spawns
        self._current, ev.deps, ev.spawns = set(), set(), 0
        try:
            return (yield)
        finally:
            self._fixtures[fixturedef] = (self._current | ev.deps, ev.spawns > 0)
            self._current |= outer[0]
            ev.deps |= outer[1]
            ev.spawns += outer[2]

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item):
        # pytest drops the request when the protocol ends: read the fixtures
        # in effect (requested by name, autouse, or fetched dynamically) now
        deps, spawned = set(), False
        for fixturedef in getattr(getattr(item, "_request", None), "_fixture_defs", {}).values():
            got = self._fixtures.get(fixturedef)
            if got:
                deps |= got[0]
                spawned = spawned or got[1]
        self._shared = (deps, spawned)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self._current = set()
        self._shared = (set(), False)
        self.evidence.test(item.nodeid)
        t0 = time.monotonic()
        try:
            return (yield)
        finally:
            dur = time.monotonic() - t0
            status = self._statuses.pop(item.nodeid, "unknown")
            ev = self.evidence
            self.records[item.nodeid] = (
                dur, status, frozenset(self._current | ev.deps | self._shared[0]))
            if ev.spawns or self._shared[1]:
                self._spawns[item.nodeid] = (ev.spawns, self._shared[1])

    def _credit_children(self) -> None:
        """Fold the child processes' reports into the tests that started
        them. A test keeps a <spawn> marker unless each process it started
        itself reported back; one that reached a process through a shared
        fixture always keeps it (the report goes to the fixture's first user)."""
        ev = self.evidence
        reports = ev.children()
        me = os.getpid()
        for test, (spawns, via_fixture) in self._spawns.items():
            mine = reports.get(test, [])
            extra = {tuple(f) for r in mine for f in r["funcs"]}
            if via_fixture or sum(r.get("parent") == me for r in mine) < spawns:
                extra.add(ev.spawn_marker())
            dur, status, deps = self.records[test]
            self.records[test] = (dur, status, deps | extra)
        mine = reports.get(journal.COLLECTION, [])
        if mine and journal.COLLECTION in self.records:
            dur, status, deps = self.records[journal.COLLECTION]
            self.records[journal.COLLECTION] = (
                dur, status, deps | {tuple(f) for r in mine for f in r["funcs"]})
        ev.cleanup()

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
        if MON:
            MON.set_events(TOOL_ID, 0)
            MON.free_tool_id(TOOL_ID)
        else:
            import threading

            sys.setprofile(None)
            threading.setprofile(None)
        self.evidence.stop()
        self._credit_children()
        t0 = time.monotonic()
        config = session.config
        jdir = config.getoption("prongs_journal") or journal.journal_dir(config.rootpath)
        key = config.getoption("prongs_journal_key") or os.environ.get("PRONGS_JOURNAL_KEY")
        info = self.run_info()
        res = journal.append(jdir, self.rootpath, self.records, info, key=key)
        if self._want_state and info["scope"] != "collect":
            # what import produced in this tree, over the whole suite's
            # modules: a later change is compared with it
            state.record(self.rootpath, res["key"],
                         state.suite_modules(Path(self.rootpath), self._modules))
        sys.stderr.write(
            f"\n[prongs] journaled run {res['key']} ({info['scope']}"
            f"{', dirty' if info['dirty_files'] else ''}): {res['n_tests']} tests"
            f" -> {res['n_funcs']} funcs ({time.monotonic() - t0:.2f}s) -> {res['path']}\n"
        )
