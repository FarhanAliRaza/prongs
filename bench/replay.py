#!/usr/bin/env python3
"""Phase 2 validation: replay real git history, check selection never misses.

Walk the last N commits oldest->newest. At each commit, run the full suite
with --fastest-cov (giving both the coverage map and per-test statuses).
For each consecutive pair (prev, cur):
  - compute the selected set from map@prev + diff(prev, cur)
  - find tests whose status changed (passed<->failed) between prev and cur
  - MISS = status-changed test, present in both runs, NOT selected
Kill criterion: any miss.

Usage: python bench/replay.py testbeds/httpx --commits 30 [--pytest-args "..."]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from fastest.select import select  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def statuses(db: Path) -> dict[str, str]:
    con = sqlite3.connect(db)
    out = dict(con.execute("SELECT test_id, status FROM tests"))
    con.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--commits", type=int, default=30)
    ap.add_argument("--pytest-args", default="")
    args = ap.parse_args()
    repo = args.repo.resolve()
    py = repo / ".venv" / "bin" / "python"
    extra = args.pytest_args.split() if args.pytest_args else []
    scratch = Path(__file__).parent.parent / "results" / f"replay-{repo.name}"
    scratch.mkdir(parents=True, exist_ok=True)
    mapfile = repo / ".fastest" / "map.sqlite"

    start_ref = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if start_ref == "HEAD":
        start_ref = git(repo, "rev-parse", "HEAD").strip()
    commits = git(
        repo, "log", "--first-parent", "--format=%H", f"-{args.commits}"
    ).split()
    commits.reverse()  # oldest first
    print(f"replaying {len(commits)} commits on {repo.name}")

    results = []
    prev_sha = None
    try:
        for i, sha in enumerate(commits):
            git(repo, "checkout", "-q", sha)
            if mapfile.exists():
                mapfile.unlink()
            t0 = time.monotonic()
            p = subprocess.run(
                [str(py), "-m", "pytest", "-q", "--fastest-cov", "-p", "no:cacheprovider"]
                + extra,
                cwd=repo, capture_output=True, text=True,
            )
            dt = time.monotonic() - t0
            if not mapfile.exists():
                print(f"  {sha[:8]} suite crashed (exit {p.returncode}), skipping")
                prev_sha = None
                continue
            shutil.copy(mapfile, scratch / f"{sha}.sqlite")
            print(f"  [{i + 1}/{len(commits)}] {sha[:8]} ran in {dt:.1f}s", flush=True)

            if prev_sha is not None:
                sel = select(scratch / f"{prev_sha}.sqlite", repo, prev_sha, sha)
                st_prev = statuses(scratch / f"{prev_sha}.sqlite")
                st_cur = statuses(mapfile)
                changed = {
                    t
                    for t in st_prev.keys() & st_cur.keys()
                    if st_prev[t] != st_cur[t]
                    and "unknown" not in (st_prev[t], st_cur[t])
                }
                sel_set = set(sel["selected"])
                misses = sorted(changed - sel_set) if sel["mode"] == "select" else []
                rec = {
                    "commit": sha,
                    "mode": sel["mode"],
                    "n_selected": len(sel_set),
                    "n_total": sel.get("n_total"),
                    "pct": round(100 * len(sel_set) / max(sel.get("n_total", 1), 1), 1),
                    "status_changed": sorted(changed),
                    "misses": misses,
                }
                results.append(rec)
                flag = "  !!! MISS !!!" if misses else ""
                print(
                    f"      diff {prev_sha[:8]}..{sha[:8]}: mode={sel['mode']} "
                    f"selected {rec['n_selected']}/{rec['n_total']} ({rec['pct']}%) "
                    f"status-changes={len(changed)}{flag}",
                    flush=True,
                )
                if misses:
                    for m in misses:
                        print(f"        MISSED: {m} ({st_prev[m]} -> {st_cur[m]})")
            prev_sha = sha
    finally:
        git(repo, "checkout", "-q", start_ref)

    out = scratch / "replay.json"
    out.write_text(json.dumps(results, indent=2))
    n_pairs = len(results)
    n_sel_mode = sum(1 for r in results if r["mode"] == "select")
    all_misses = [m for r in results for m in r["misses"]]
    sel_pcts = [r["pct"] for r in results if r["mode"] == "select"]
    print(f"\n=== {n_pairs} commit pairs | select-mode: {n_sel_mode}, "
          f"run-all: {n_pairs - n_sel_mode}")
    if sel_pcts:
        print(f"=== selected%% in select-mode: min {min(sel_pcts)} "
              f"median {sorted(sel_pcts)[len(sel_pcts) // 2]} max {max(sel_pcts)}")
    print(f"=== MISSES: {len(all_misses)} {'<<< KILL CRITERION HIT' if all_misses else '(zero — pass)'}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
