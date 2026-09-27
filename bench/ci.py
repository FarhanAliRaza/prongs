#!/usr/bin/env python3
"""The CI artifact flow, replayed: every job is a fresh clone of one commit,
and all that survives between jobs is artifact directories (`fastest ci
save` / `fastest ci restore`), the way a CI cache and uploaded artifacts
carry them.

Walk the last N first-parent commits of a testbed, oldest first. Each commit
is a pull request against its parent, then merged:

  bootstrap  a fresh clone of the oldest commit; the artifact store is
             empty, so `fastest run` runs everything and records it; `ci
             save` writes the first map artifact
  PR job     a fresh clone of the commit (--shallow: depth 1, the way CI
             checks out, and `ci restore` deepens it); `ci restore` the
             newest map artifact; `fastest run`, the timed step and the gate;
             then, in shadow mode, `fastest audit` — the full suite, every
             status change the selection skipped, each re-run alone — as
             ground truth; `ci save --no-map` its journal files
  main job   on every --map-every K-th commit: a fresh clone; `ci restore`
             the newest map and every PR job's journals since the last main
             job; `fastest rollup`; `ci save` the next map artifact. Its full
             run is the PR job's audit run, recorded (same commit, same
             tree), rather than a second full run of the same code. Between
             main jobs, PR jobs select from a map up to K commits behind.

Reported per PR job: the selection (mode, selected/total, reasons), the
exit code and wall time of `fastest run` next to the full suite's, what
`ci restore` fetched and how long it took, misses. Kill criterion: any
first-order miss.

Usage: python bench/ci.py testbeds/httpx --commits 20 [--shallow] [--map-every K]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# a job starts from the runner's environment, not the harness's: no fastest
# settings, and no proxy (httpx's proxy tests read these, and fail on a
# sandbox's agent proxy that no CI runner sets)
CLEAN_ENV = {"PYTEST_ADDOPTS", "FASTEST_COV", "FASTEST_DIR", "FASTEST_JOURNAL",
             "FASTEST_JOURNAL_KEY"}
PROXY_ENV = {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def checkout(origin: Path, sha: str, dest: Path, shallow: bool) -> Path:
    """A CI job's checkout: one commit fetched from the origin, detached."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    git(dest, "init", "-q")
    git(dest, "remote", "add", "origin", origin.as_uri())
    git(dest, "fetch", "-q", "--no-tags", *(["--depth=1"] if shallow else []), "origin", sha)
    git(dest, "checkout", "-q", "--detach", "FETCH_HEAD")
    return dest


class Job:
    def __init__(self, path: Path, py: str):
        self.path, self.py = path, py
        self.env = {k: v for k, v in os.environ.items()
                    if k not in CLEAN_ENV and k.lower() not in PROXY_ENV}

    def fastest(self, *args: str) -> tuple[int, dict, float]:
        t0 = time.monotonic()
        p = subprocess.run([self.py, "-m", "fastest", *args], cwd=self.path, env=self.env,
                           capture_output=True, text=True)
        wall = time.monotonic() - t0
        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError:
            out = {"error": "no JSON", "stderr": p.stderr[-2000:]}
        return p.returncode, out, round(wall, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--commits", type=int, default=20)
    ap.add_argument("--shallow", action="store_true", help="PR jobs check out at depth 1")
    ap.add_argument("--map-every", type=int, default=1, metavar="K",
                    help="a main job (a new map artifact) on every K-th commit")
    ap.add_argument("--work", type=Path, default=None, help="scratch directory for the jobs")
    args = ap.parse_args()
    origin = args.repo.resolve()
    py = str(origin / ".venv" / "bin" / "python")
    work = (args.work or Path(tempfile.mkdtemp(prefix="fastest-ci-"))).resolve()
    store = work / "artifacts"
    store.mkdir(parents=True, exist_ok=True)
    tag = ("-shallow" if args.shallow else "") + (f"-every{args.map_every}" if args.map_every > 1 else "")
    out_path = Path(__file__).parent.parent / "results" / f"ci-{origin.name}{tag}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    commits = git(origin, "log", "--first-parent", "--format=%H", f"-{args.commits}").split()
    commits.reverse()  # oldest first
    print(f"CI replay of {len(commits)} commits on {origin.name}: PR jobs "
          f"{'shallow (depth 1)' if args.shallow else 'full clones'}, a main job every "
          f"{args.map_every} commit(s); work in {work}", flush=True)

    # bootstrap: an empty artifact store, so everything runs and is recorded
    job = Job(checkout(origin, commits[0], work / "job", shallow=False), py)
    code, res, wall = job.fastest("run")
    boot = {"commit": commits[0], "exit": code, "mode": res.get("selection", {}).get("mode"),
            "ran": res.get("summary", {}).get("ran"), "wall_s": wall}
    maps = [store / "map-0000"]
    _, saved, _ = job.fastest("ci", "save", str(maps[-1]))
    boot["map"] = saved.get("map")
    print(f"  bootstrap {commits[0][:8]}: {boot['mode']}, ran {boot['ran']} in {wall}s "
          f"-> map of {boot['map']['tests']} tests", flush=True)

    results, since_main = [], []
    for i, sha in enumerate(commits[1:], start=1):
        is_main = i % args.map_every == 0
        job = Job(checkout(origin, sha, work / "job", shallow=args.shallow), py)
        _, restored, restore_wall = job.fastest("ci", "restore", str(maps[-1]))
        code, run, run_wall = job.fastest("run")
        # shadow mode: the full suite as ground truth (recorded on main commits,
        # where it doubles as the main job's full run)
        audit_args = ["audit", "--no-rollup"] + (["--record"] if is_main else [])
        audit_code, aud, _ = job.fastest(*audit_args)
        pr_art = store / f"pr-{i:04d}"
        job.fastest("ci", "save", "--no-map", str(pr_art))
        since_main.append(pr_art)
        sel, summ = run.get("selection", {}), run.get("summary", {})
        rec = {
            "commit": sha, "parent": commits[i - 1],
            "restore": {"wall_s": restore_wall, "map": (restored.get("map") or {}).get("rollup_commit"),
                        "fetches": len(restored.get("history", {}).get("fetches", [])),
                        "still_missing": restored.get("history", {}).get("still_missing"),
                        "warnings": restored.get("warnings", [])},
            "map_age": (run.get("skip_receipt") or {}).get("map_age", {}).get("commits_behind_head"),
            "mode": sel.get("mode"), "run_all_reasons": sel.get("run_all_reasons", []),
            "n_selected": sel.get("n_selected"), "n_total": sel.get("n_total"),
            "exit": code, "status": summ.get("status"), "ran": summ.get("ran"),
            "failed": summ.get("failed"), "conserved": run.get("conservation", {}).get("conserved"),
            "run_wall_s": run_wall, "exec_wall_s": summ.get("exec_wall_s"),
            "full_wall_s": aud.get("full_run", {}).get("wall_s"),
            "audit": {"verdict": aud.get("verdict"), "exit": audit_code,
                      "status_changes": aud.get("status_changes"),
                      "misses": aud.get("misses", []),
                      "pollution": [p["test"] for p in aud.get("pollution", [])],
                      "drift": [d["test"] for d in aud.get("drift", [])],
                      "error": aud.get("error")},
        }
        if rec["n_total"]:
            rec["pct"] = round(100 * rec["n_selected"] / rec["n_total"], 1)
        if "error" in run:
            rec["run_error"] = run["error"]
        if is_main:
            main_job = Job(checkout(origin, sha, work / "main", shallow=False), py)
            _, restored_main, _ = main_job.fastest("ci", "restore", str(maps[-1]),
                                                   *map(str, since_main))
            _, rolled, roll_wall = main_job.fastest("rollup")
            maps.append(store / f"map-{i:04d}")
            _, saved, _ = main_job.fastest("ci", "save", str(maps[-1]))
            rec["main_job"] = {"journals": restored_main.get("journals", {}),
                               "rolled_up": rolled.get("rolled_up"), "rollup_wall_s": roll_wall,
                               "map_bytes": (saved.get("map") or {}).get("bytes")}
            since_main = []
            shutil.rmtree(work / "main", ignore_errors=True)
        results.append(rec)
        miss = "  !!! MISS !!!" if rec["audit"]["misses"] else ""
        print(f"  [{i}/{len(commits) - 1}] {sha[:8]} map age {rec['map_age']}: {rec['mode']} "
              f"{rec['n_selected']}/{rec['n_total']} ({rec.get('pct')}%), exit {code}, "
              f"run {run_wall}s vs full {rec['full_wall_s']}s, restore {restore_wall}s "
              f"({rec['restore']['fetches']} fetches), audit {rec['audit']['verdict']} "
              f"changes={(rec['audit']['status_changes'] or {}).get('total')}{miss}", flush=True)
        for reason in rec["run_all_reasons"]:
            print(f"        run-all: {reason}")
        for m in rec["audit"]["misses"]:
            print(f"        MISSED: {m['test']} ({m['before']} -> {m['after']})")
        if rec["audit"]["error"] or rec.get("run_error"):
            print(f"        error: {rec['audit']['error'] or rec.get('run_error')}")

    out_path.write_text(json.dumps({"bootstrap": boot, "jobs": results}, indent=2))
    ok = [r for r in results if r["audit"]["verdict"] in ("pass", "miss")]
    misses = [m for r in results for m in r["audit"]["misses"]]
    sel = [r for r in ok if r["mode"] == "select"]
    print(f"\n=== {len(results)} PR jobs ({len(ok)} audited), select-mode {len(sel)}, "
          f"run-all {sum(r['mode'] == 'run_all' for r in ok)}")
    if sel:
        pcts = sorted(r["pct"] for r in sel)
        print(f"=== selected%: median {statistics.median(pcts)}, max {pcts[-1]}")
    if ok:
        run_s = sum(r["run_wall_s"] for r in ok)
        full_s = sum(r["full_wall_s"] or 0 for r in ok)
        print(f"=== test step: fastest run {run_s:.1f}s total vs full suite {full_s:.1f}s "
              f"(median {statistics.median(r['run_wall_s'] for r in ok)}s vs "
              f"{statistics.median(r['full_wall_s'] or 0 for r in ok)}s per job)")
        print(f"=== restore: median {statistics.median(r['restore']['wall_s'] for r in ok)}s, "
              f"max {max(r['restore']['wall_s'] for r in ok)}s")
    print(f"=== MISSES: {len(misses)} "
          f"{'<<< KILL CRITERION HIT' if misses else '(zero: pass)'}; pollution "
          f"{sum(len(r['audit']['pollution']) for r in results)}, drift "
          f"{sum(len(r['audit']['drift']) for r in results)}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    sys.exit(main())
