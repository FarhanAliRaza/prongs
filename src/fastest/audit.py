"""Shadow mode: what selection would skip, checked against a full run.

`fastest audit [--base REV]`:

  1. select exactly as `fastest run` would (--base REV, else the evidence)
  2. run the FULL suite in a subprocess with the recorder on (FASTEST_COV=1)
     into a temporary database, so an audit never feeds the map it audits
     (record=True appends the run to the real journal instead; history
     replay uses that to walk commits)
  3. diff every live test's status against the baseline: the status the base
     map recorded, or statuses the caller supplies (replay passes the
     previous commit's full run when the map is deliberately left behind)
  4. a test whose status changed and was not selected is a miss. Each miss is
     re-run alone on the same tree: still different from its baseline -> a
     first-order miss, the selector's fault and the kill criterion; back to
     its baseline -> second-order pollution from an already-selected failure
     (a skipped cleanup, an unraisable exception landing on a later test),
     which fork-per-test isolation fixes, not selection

Every miss carries the receipt that skipped it. bench/mutate.py and
bench/replay.py are loops around audit(): mutate or check out, then audit.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastest import mapdb, provenance
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


def _last_run_id(db: Path) -> int:
    if not db.exists():
        return 0
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def _run_statuses(db: Path, after_run: int) -> dict[str, str]:
    """{test_id: status} observed by the newest run after `after_run`."""
    if not db.exists():
        return {}  # pytest died before the recorder wrote anything
    con = sqlite3.connect(db)
    try:
        run_id = con.execute("SELECT MAX(id) FROM runs WHERE id > ?", (after_run,)).fetchone()[0]
        if run_id is None:
            return {}
        return {
            t: s for t, s in con.execute(
                "SELECT t.test_id, o.status FROM observations o JOIN tests t ON t.id=o.test_id "
                "WHERE o.run_id=?", (run_id,)
            ) if t != mapdb.COLLECTION
        }
    finally:
        con.close()


def observe(repo: Path, python: str = sys.executable, pytest_args=(), targets=(),
            record: bool = False) -> dict:
    """Run pytest with the recorder on and return what it observed:
    {'exit', 'wall_s', 'statuses': {test_id: status}, ['output']}. The
    statuses come from the recorder's own database, never from parsing
    pytest's output (poc-results finding 4)."""
    tmp = Path(tempfile.mkdtemp(prefix="fastest-audit-"))
    db = repo / ".fastest" / "map.sqlite" if record else tmp / "map.sqlite"
    before = _last_run_id(db)
    env = dict(os.environ, FASTEST_COV="1", FASTEST_DB=str(db))
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            pytest_cmd(python, pytest_args, targets), cwd=repo, env=env,
            capture_output=True, text=True,
        )
        wall = time.monotonic() - t0
        statuses = _run_statuses(db, before)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    out = {"exit": p.returncode, "wall_s": round(wall, 2), "statuses": statuses}
    if p.returncode not in (0, 1):
        out["output"] = (p.stdout + p.stderr)[-2000:]
    return out


def map_statuses(con) -> dict[str, str]:
    """{test_id: status} for every live test in the map."""
    return dict(con.execute(
        "SELECT test_id, status FROM tests WHERE retired_run IS NULL "
        "AND deps_set IS NOT NULL AND test_id != ?", (mapdb.COLLECTION,),
    ))


def status_changes(baseline: dict[str, str], current: dict[str, str], repo: Path) -> dict:
    """{test_id: (before, after)} for every test whose outcome differs. A test
    missing from the current run is 'error' when its whole module produced
    no result (it failed to collect); it is not a change when its module ran
    without it (renamed or deleted) or no longer exists."""
    ran_modules = {t.split("::", 1)[0] for t in current}
    out: dict[str, tuple[str, str]] = {}
    for t, before in baseline.items():
        if before in NOT_AN_OUTCOME:
            continue
        mod = t.split("::", 1)[0]
        after = current.get(t)
        if after is None:
            if mod in ran_modules or not (repo / mod).exists():
                continue
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
        "SELECT last_run, deps_set FROM tests WHERE test_id=?", (test_id,)
    ).fetchone()
    if row is None:
        return {"rule": "not in the map"}
    last_run, deps_set = row
    deps = con.execute(
        "SELECT f.path, fn.qualname FROM dep_members m JOIN funcs fn ON fn.id=m.func_id "
        "JOIN files f ON f.id=fn.file_id WHERE m.set_id=?", (deps_set,),
    ).fetchall() if deps_set is not None else []
    run = con.execute("SELECT commit_sha, scope FROM runs WHERE id=?", (last_run,)).fetchone()
    changed = sel.get("changed_functions", [])
    touched = {f.split("::", 1)[0] for f in changed} | set(sel.get("changed_files_wholesale", []))
    return {
        "rule": "no overlap between this test's coverage map and the diff",
        "observed_by_run": last_run,
        "observed_at": (run[0] or "")[:8] if run else None,
        "n_deps": len(deps),
        "deps_in_changed_files": sorted(f"{p}::{q}" for p, q in deps if p in touched)[:10],
        "changed_functions": changed[:10],
        "changed_files_wholesale": sel.get("changed_files_wholesale", [])[:10],
    }


def audit(repo: Path, base: str | None = None, *, python: str = sys.executable,
          pytest_args=(), baseline: dict[str, str] | None = None, isolate: bool = True,
          record: bool = False, max_isolate: int = MAX_ISOLATE) -> dict:
    """Select, run everything, report every status change the selection
    skipped. The result's `statuses` (every test's status in the full run)
    is for harnesses that chain audits; the CLI drops it."""
    t0 = time.monotonic()
    repo = Path(repo).resolve()
    db = repo / ".fastest" / "map.sqlite"
    sel = select(db, repo, base)
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
                "changed_functions": sel["changed_functions"][:20],
            },
            "full_run": {
                "exit": run["exit"], "wall_s": run["wall_s"],
                "n_results": len(run["statuses"]), "recorded": record,
            },
            "baseline": {"source": "map" if baseline is None else "given",
                         "n_tests": len(base_statuses)},
        }
        if run["exit"] not in (0, 1):
            # the full run itself went wrong: its statuses are no verdict
            out["full_run"]["output"] = run.get("output", "")
            out.update(verdict="error", misses=[], pollution=[], statuses=run["statuses"],
                       error=f"full run exited {run['exit']}")
            return out
        changes = status_changes(base_statuses, run["statuses"], repo)
        chosen = planned(sel, base_statuses)
        unselected = sorted(t for t in changes if t not in chosen)
        misses, pollution = [], []
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
    finally:
        con.close()
    out.update(
        status_changes={"total": len(changes), "selected": len(changes) - len(unselected),
                        "unselected": len(unselected)},
        misses=misses,
        pollution=pollution,
        verdict="miss" if misses else "pass",
        wall_s=round(time.monotonic() - t0, 2),
        statuses=run["statuses"],
    )
    return out
