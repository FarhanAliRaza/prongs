#!/usr/bin/env python3
"""Phase 2 validation: replay real git history through `prongs audit`.

Walk the last N first-parent commits, oldest first. The map is built once, by
a full recorded run at the oldest commit; every later commit is checked out
and audited against its parent (`audit --base <parent>`): select from the map
and the parent..commit diff, run the full suite, diff every test's status
against the baseline, re-run unselected changes alone.

  default   each audit records its full run, so the map follows the walk and
            every pair is judged with map@parent: the classic replay
  --stale   the map stays at the oldest commit and each audit's baseline is
            the previous audit's full run: selection works from a map that is
            several revisions behind HEAD, and the map-staleness rules in
            select.py are what keep it sound

Kill criterion: any first-order miss.

Usage: python bench/replay.py testbeds/httpx --commits 30 [--stale] [--pytest-args "..."]
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from prongs import journal  # noqa: E402
from prongs.audit import audit, observe, run_alone  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def build_map(repo: Path, py: str, extra: list[str]) -> int:
    """A fresh map from one full recorded run of the checked-out commit (the
    next audit rolls its journal file up)."""
    db = journal.map_path(repo)
    for p in db.parent.glob(db.name + "*"):
        p.unlink()
    shutil.rmtree(journal.journal_dir(repo), ignore_errors=True)
    return subprocess.run(
        [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--prongs-cov", *extra],
        cwd=repo, capture_output=True, text=True,
    ).returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--commits", type=int, default=30)
    ap.add_argument("--stale", action="store_true",
                    help="keep the map at the oldest commit instead of following the walk")
    ap.add_argument("--pytest-args", default="")
    args = ap.parse_args()
    repo = args.repo.resolve()
    py = str(repo / ".venv" / "bin" / "python")
    extra = shlex.split(args.pytest_args)
    tag = "-stale" if args.stale else ""
    out = Path(__file__).parent.parent / "results" / f"replay-{repo.name}{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    start_ref = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if start_ref == "HEAD":
        start_ref = git(repo, "rev-parse", "HEAD").strip()
    commits = git(repo, "log", "--first-parent", "--format=%H", f"-{args.commits}").split()
    commits.reverse()  # oldest first
    print(f"replaying {len(commits)} commits on {repo.name}"
          f"{' (map stays at ' + commits[0][:8] + ')' if args.stale else ''}")

    results = []
    try:
        git(repo, "checkout", "-q", commits[0])
        rc = build_map(repo, py, extra)
        if rc not in (0, 1):
            sys.exit(f"suite crashed building the map at {commits[0][:8]} (exit {rc})")
        prev, baseline = commits[0], None
        for i, sha in enumerate(commits[1:], start=1):
            git(repo, "checkout", "-q", sha)
            if prev is None:
                # the previous commit's suite crashed: re-establish a baseline
                if args.stale:
                    baseline = observe(repo, py, extra)["statuses"]
                elif build_map(repo, py, extra) not in (0, 1):
                    print(f"  [{i}] {sha[:8]} suite crashed, skipping")
                    continue
                prev = sha
                print(f"  [{i}] {sha[:8]} baseline re-established")
                continue
            def control(tests, prev=prev, sha=sha):
                """The misses run alone on the parent commit, same checkout."""
                git(repo, "checkout", "-q", prev)
                try:
                    return run_alone(repo, py, extra, tests)
                finally:
                    git(repo, "checkout", "-q", sha)

            res = audit(repo, prev, python=py, pytest_args=extra, control=control,
                        baseline=baseline if args.stale else None, record=not args.stale)
            if res.get("verdict") == "error":
                print(f"  [{i}] {sha[:8]} audit error ({res.get('error')}), skipping pair")
                prev = baseline = None
                continue
            sel = res["selection"]
            rec = {
                "commit": sha,
                "parent": prev,
                "map_age": i if args.stale else 1,
                "mode": sel["mode"],
                "run_all_reasons": sel["run_all_reasons"],
                "n_selected": sel["n_selected"],
                "n_total": sel["n_total"],
                "pct": round(100 * sel["n_selected"] / max(sel["n_total"], 1), 1),
                "status_changes": res["status_changes"],
                "misses": res["misses"],
                "pollution": [p["test"] for p in res["pollution"]],
                "drift": [d["test"] for d in res["drift"]],
                "broadened": sel["broadened"],
                "map_age_receipt": sel["map_age"],
                "wall_s": res["wall_s"],
            }
            results.append(rec)
            flag = "  !!! MISS !!!" if rec["misses"] else ""
            print(
                f"  [{i}/{len(commits) - 1}] {prev[:8]}..{sha[:8]}: mode={rec['mode']} "
                f"selected {rec['n_selected']}/{rec['n_total']} ({rec['pct']}%) "
                f"status-changes={rec['status_changes']['total']} "
                f"pollution={len(rec['pollution'])} drift={len(rec['drift'])}{flag}",
                flush=True,
            )
            for reason in rec["run_all_reasons"]:
                print(f"        run-all: {reason}")
            for reason in rec["broadened"]:
                print(f"        broadened: {reason}")
            for m in rec["misses"]:
                print(f"        MISSED: {m['test']} ({m['before']} -> {m['after']})")
            if args.stale:
                baseline = res["statuses"]
            prev = sha
    finally:
        git(repo, "checkout", "-q", start_ref)

    out.write_text(json.dumps(results, indent=2))
    n_pairs = len(results)
    n_sel_mode = sum(1 for r in results if r["mode"] == "select")
    all_misses = [m for r in results for m in r["misses"]]
    sel_pcts = sorted(r["pct"] for r in results if r["mode"] == "select")
    print(f"\n=== {n_pairs} commit pairs | select-mode: {n_sel_mode}, "
          f"run-all: {n_pairs - n_sel_mode}")
    if sel_pcts:
        print(f"=== selected% in select-mode: min {sel_pcts[0]} "
              f"median {sel_pcts[len(sel_pcts) // 2]} max {sel_pcts[-1]}")
    print(f"=== MISSES: {len(all_misses)} "
          f"{'<<< KILL CRITERION HIT' if all_misses else '(zero — pass)'}, "
          f"pollution: {sum(len(r['pollution']) for r in results)}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
