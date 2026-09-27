"""Shadow mode: what selection would skip, checked against a full run.

`fastest audit [--base REV]`:

  1. select exactly as `fastest run` would (--base REV, else the evidence),
     after rolling up pending journal files
  2. run the FULL suite in a subprocess with the recorder on (FASTEST_COV=1)
     into a temporary journal, so an audit never feeds the map it audits
     (record=True appends the run to the real journal instead; history replay
     uses that to walk commits)
  3. diff every live test's status against the baseline: the status the base
     map recorded, or statuses the caller supplies (replay passes the
     previous commit's full run when the map is deliberately left behind)
  4. a test whose status changed and was not selected is a miss. Each miss is
     re-run alone on the same tree: still different from its baseline -> a
     first-order miss, the selector's fault and the kill criterion; back to
     its baseline -> second-order pollution from an already-selected failure
     (a skipped cleanup, an unraisable exception landing on a later test),
     which fork-per-test isolation fixes, not selection
  5. with a `control`, each first-order miss is also run alone on the base
     code, in the same checkout: if it differs from its baseline there too,
     the change did not move it — the environment did (state a previous run
     left behind in an ignored directory, a clock) — and it is reported as
     drift, not a miss. Harnesses that own the working tree supply their
     own; `fastest audit --control` uses checkout_control(), which checks
     out the commit each miss's baseline was observed on, in place

Every miss carries the receipt that skipped it; a known-flaky test that
flips is reported under `flaky` with its history, not as a miss.
bench/mutate.py and
bench/replay.py are loops around audit(): mutate or check out, then audit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastest import config, journal, mapdb, provenance
from fastest.select import select

NOT_AN_OUTCOME = {"unknown", "collection"}  # statuses that are never compared
MAX_ISOLATE = 25  # re-running misses alone is one pytest start-up each


def pytest_cmd(python: str, pytest_args=(), targets=()) -> list[str]:
    return [
        python, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "--tb=no",
        # a module that fails to import must not take the rest of the suite
        # with it: every other test still needs a status to compare
        "--continue-on-collection-errors", *pytest_args, *targets,
    ]


def observe(repo: Path, python: str = sys.executable, pytest_args=(), targets=(),
            record: bool = False) -> dict:
    """Run pytest with the recorder on and return what it observed:
    {'exit', 'wall_s', 'statuses': {test_id: status}, 'collect_errors',
    'collect_skipped', 'journal', ['output']}. Everything comes from the run's
    own journal file, never from parsing pytest's output (poc-results finding
    4). Unless `record`, that file goes to a temporary directory and is
    discarded."""
    tmp = Path(tempfile.mkdtemp(prefix="fastest-audit-"))
    jdir = journal.journal_dir(repo) if record else tmp
    key = journal.new_key()
    env = dict(os.environ, FASTEST_COV="1", FASTEST_JOURNAL=str(jdir), FASTEST_JOURNAL_KEY=key)
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            pytest_cmd(python, pytest_args, targets), cwd=repo, env=env,
            capture_output=True, text=True,
        )
        wall = time.monotonic() - t0
        path = journal.find(jdir, key)  # absent: pytest died before the recorder wrote
        run = journal.read_run(path) if path else {}
        out = {
            "exit": p.returncode,
            "wall_s": round(wall, 2),
            "statuses": journal.statuses(path) if path else {},
            "collect_errors": run.get("collect_errors", {}),
            "collect_skipped": run.get("collect_skipped", []),
            "journal": key if record and path else None,
            # no journal file: pytest died before its session finished (a
            # start-up crash exits 1 like a test failure), so nothing ran
            "recorded_anything": path is not None,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if p.returncode not in (0, 1) or not out["recorded_anything"]:
        out["output"] = (p.stdout + p.stderr)[-2000:]
    return out


def run_alone(repo: Path, python: str, pytest_args, tests) -> dict[str, str]:
    """Each test in its own pytest session: {test_id: status, or 'absent'}."""
    return {
        t: observe(repo, python, pytest_args, targets=[t])["statuses"].get(t, "absent")
        for t in tests
    }


def map_statuses(con) -> dict[str, str]:
    """{test_id: status} for every live test in the map."""
    return dict(con.execute(
        "SELECT test_id, status FROM tests WHERE retired_run IS NULL "
        "AND last_run IS NOT NULL AND test_id != ?", (mapdb.COLLECTION,),
    ))


def status_changes(baseline: dict[str, str], run: dict, repo: Path) -> dict:
    """{test_id: (before, after)} for every test whose outcome differs. A test
    missing from the run is 'skipped' when its module was skipped at import,
    'error' when its module failed to collect or produced no result at all;
    it is no change when its module ran without it (renamed or deleted) or
    no longer exists."""
    current = run["statuses"]
    ran_modules = {t.split("::", 1)[0] for t in current}
    errored = set(run.get("collect_errors", {}))
    skipped = set(run.get("collect_skipped", []))
    out: dict[str, tuple[str, str]] = {}
    for t, before in baseline.items():
        if before in NOT_AN_OUTCOME:
            continue
        mod = t.split("::", 1)[0]
        after = current.get(t)
        if after is None:
            if not (repo / mod).exists():
                continue
            if mod in skipped:
                after = "skipped"
            elif mod in ran_modules and mod not in errored:
                continue
            else:
                after = "error"
        if after not in NOT_AN_OUTCOME and after != before:
            out[t] = (before, after)
    return out


def planned(sel: dict, tests) -> set[str]:
    """Every test `fastest run` would execute for this selection: the
    selected ids, plus every test in a module targeted as a whole file."""
    files = set(sel.get("selected_files", {}))
    return set(sel["selected"]) | {t for t in tests if t.split("::", 1)[0] in files}


def skip_receipt_for(con, sel: dict, test_id: str) -> dict:
    """Why the selector skipped this test: the rule, the evidence it was
    applied to, and which of the test's recorded dependencies sit in the
    files the diff touched (the ones it was cleared against)."""
    row = con.execute(
        "SELECT deps_run, deps_set FROM tests WHERE test_id=?", (test_id,)
    ).fetchone()
    if row is None:
        return {"rule": "not in the map"}
    deps_run, deps_set = row
    deps = con.execute(
        "SELECT f.path, fn.qualname FROM dep_members m JOIN funcs fn ON fn.id=m.func_id "
        "JOIN files f ON f.id=fn.file_id WHERE m.set_id=?", (deps_set,),
    ).fetchall() if deps_set is not None else []
    run = con.execute("SELECT commit_sha, scope FROM runs WHERE id=?", (deps_run,)).fetchone()
    changed = sel.get("changed_functions", [])
    touched = {f.split("::", 1)[0] for f in changed} | set(sel.get("changed_files_wholesale", []))
    return {
        "rule": "no overlap between this test's coverage map and the diff",
        "observed_by_run": deps_run,
        "observed_at": (run[0] or "")[:8] if run else None,
        "n_deps": len(deps),
        "deps_in_changed_files": sorted(f"{p}::{q}" for p, q in deps if p in touched)[:10],
        "changed_functions": changed[:10],
        "changed_files_wholesale": sel.get("changed_files_wholesale", [])[:10],
    }


class ControlError(Exception):
    """A control run that cannot be done safely."""


def checkout_control(repo: Path, python: str = sys.executable, pytest_args=()):
    """A control for `fastest audit --control`: run each test alone on the
    commit its baseline status was observed on, in this checkout — ignored
    files, the state a previous run left behind, stay exactly as they are,
    which is the point. It checks out each commit detached and puts HEAD
    back afterwards, so it refuses a working tree with changes (commit
    them, or run it in CI) and a baseline observed on a tree with changes
    (that tree cannot be checked out)."""
    repo = Path(repo)

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    def control(tests) -> dict[str, str]:
        con = mapdb.connect(str(journal.map_path(repo)))
        try:
            base: dict[str, list[str]] = {}
            for t in tests:
                row = con.execute(
                    "SELECT r.commit_sha, r.dirty_files FROM tests t JOIN runs r "
                    "ON r.id = t.last_run WHERE t.test_id = ?", (t,)).fetchone()
                if row and row[0] and row[1] in (None, "{}"):
                    base.setdefault(row[0], []).append(t)
        finally:
            con.close()
        if not base:
            raise ControlError("no miss has a baseline observed on a clean commit")
        if provenance.dirty_files(repo):
            raise ControlError("the working tree has changes: commit them (or run the "
                               "control in CI) so the base commit can be checked out")
        branch = git("symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
        back = branch or provenance.git_head(repo)
        out: dict[str, str] = {}
        try:
            for commit, ts in sorted(base.items()):
                p = git("checkout", "-q", "--detach", commit)
                if p.returncode:
                    raise ControlError(f"cannot check out {commit[:8]}: {p.stderr.strip()[-300:]}")
                out.update(run_alone(repo, python, pytest_args, ts))
        finally:
            git("checkout", "-q", back)
        return out

    return control


def audit(repo: Path, base: str | None = None, *, python: str = sys.executable,
          pytest_args=(), baseline: dict[str, str] | None = None, isolate: bool = True,
          record: bool = False, rollup: bool = True, max_isolate: int = MAX_ISOLATE,
          history_window: int | None = None, max_map_age: int | None = None,
          flaky_window: int | None = None, control=None) -> dict:
    """Select, run everything, report every status change the selection
    skipped. Pending journal files are rolled up first, as `fastest run`
    would. `control(tests)`, if given, returns each test's status run alone
    on the base code (see step 5). The result's `statuses` (every test's
    status in the full run) is for harnesses that chain audits; the CLI
    drops it."""
    t0 = time.monotonic()
    repo = Path(repo).resolve()
    db = journal.map_path(repo)
    if rollup:
        mapdb.rollup(db, journal.journal_dir(repo))
    cfg = config.settings(repo, history_window=history_window, max_map_age=max_map_age,
                          flaky_window=flaky_window)
    sel = select(db, repo, base, history_window=cfg["history_window"],
                 max_map_age=cfg["max_map_age"], flaky_window=cfg["flaky_window"])
    if "error" in sel:
        return {"error": sel["error"], "verdict": "error"}
    con = mapdb.connect(str(db))
    try:
        base_statuses = map_statuses(con) if baseline is None else dict(baseline)
        run = observe(repo, python, pytest_args, record=record)
        out = {
            "base": base or "evidence",
            "head": provenance.git_head(repo),
            "selection": {
                "mode": sel["mode"],
                "n_total": sel["n_total"],
                "n_selected": sel["n_selected"],
                "n_skipped": sel["n_skipped"],
                "run_all_reasons": sel.get("reasons", []),
                "broadened": sel.get("broadened", []),
                "map_age": sel["evidence"]["map_age"],
                "unmapped_tests": sel["evidence"]["unmapped_tests"],
                "changed_functions": sel["changed_functions"][:20],
            },
            "full_run": {
                "exit": run["exit"], "wall_s": run["wall_s"],
                "n_results": len(run["statuses"]), "journal": run["journal"],
                "collect_errors": run["collect_errors"],
            },
            "baseline": {"source": "map" if baseline is None else "given",
                         "n_tests": len(base_statuses)},
        }
        if run["exit"] not in (0, 1) or not run["recorded_anything"]:
            # the full run itself went wrong: its statuses are no verdict
            out["full_run"]["output"] = run.get("output", "")
            out.update(verdict="error", misses=[], pollution=[], flaky=[], drift=[],
                       statuses=run["statuses"],
                       error=f"full run exited {run['exit']}" if run["recorded_anything"]
                       else "full run recorded nothing: pytest died before its session finished")
            return out
        changes = status_changes(base_statuses, run, repo)
        chosen = planned(sel, base_statuses)
        unselected = sorted(t for t in changes if t not in chosen)
        misses, pollution, flaky = [], [], []
        for t in [t for t in unselected if t in sel["flaky"]]:
            # a known flake flipping is noise, not a selection miss
            before, after = changes[t]
            flaky.append({"test": t, "before": before, "after": after,
                          "history": sel["flaky"][t]["history"]})
        unselected = [t for t in unselected if t not in sel["flaky"]]
        for i, t in enumerate(unselected):
            before, after = changes[t]
            entry = {"test": t, "before": before, "after": after,
                     "receipt": skip_receipt_for(con, sel, t)}
            if isolate and i < max_isolate:
                alone = observe(repo, python, pytest_args, targets=[t])["statuses"]
                entry["alone"] = alone.get(t, "absent")
                if entry["alone"] == before:
                    pollution.append(entry)
                    continue
            elif isolate:
                entry["alone"] = None  # over the isolation cap: counted as a miss
            misses.append(entry)
        drift = []
        if control and misses:
            try:
                on_base = control([m["test"] for m in misses if m.get("alone") is not None])
            except ControlError as e:
                on_base = {}
                out["control_error"] = str(e)
            for m in [m for m in misses if m["test"] in on_base]:
                m["on_base"] = on_base[m["test"]]
                if m["on_base"] != m["before"]:  # it moved without the change
                    drift.append(m)
            misses = [m for m in misses if m not in drift]
    finally:
        con.close()
    out.update(
        status_changes={"total": len(changes),
                        "selected": len(changes) - len(unselected) - len(flaky),
                        "unselected": len(unselected) + len(flaky)},
        misses=misses,
        pollution=pollution,
        flaky=flaky,
        drift=drift,
        verdict="miss" if misses else "pass",
        wall_s=round(time.monotonic() - t0, 2),
        statuses=run["statuses"],
    )
    return out
