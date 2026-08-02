"""Milestone 0: benchmark runner comparing pytest, pytest-xdist and ztest.

    python benchmarks/bench.py --suite testbeds/tiny --jobs 4 --repeat 3 \\
        --out results/tiny.json

Records wall-clock duration, time-to-first-test, exit code and peak RSS per
runner configuration, as JSON.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ZTEST_PYTHON = str(Path(__file__).resolve().parents[1] / "python")


def run_once(cmd: list[str], cwd: str | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = ZTEST_PYTHON + os.pathsep + env.get("PYTHONPATH", "")
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    wall = time.monotonic() - start
    after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return {
        "wall_s": round(wall, 4),
        "exit": proc.returncode,
        "peak_rss_kb_delta": after - before,
        "tail": proc.stdout.strip().split("\n")[-1] if proc.stdout.strip() else "",
    }


def runners(jobs: int, suite: str) -> dict[str, list[str]]:
    py = sys.executable
    base = [suite, "-q", "-p", "no:cacheprovider", "--tb=no"]
    return {
        "pytest": [py, "-m", "pytest", *base],
        f"xdist-load-{jobs}": [py, "-m", "pytest", "-n", str(jobs), "--dist=load", *base],
        f"xdist-worksteal-{jobs}": [py, "-m", "pytest", "-n", str(jobs), "--dist=worksteal", *base],
        f"ztest-proto-{jobs}": [py, "-m", "ztest_py", "run", "-j", str(jobs), "--", *base],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", default=None)
    parser.add_argument("--skip", default="", help="comma-separated runner substrings to skip")
    args = parser.parse_args()

    skip = [s for s in args.skip.split(",") if s]
    results: dict[str, list[dict]] = {}
    for name, cmd in runners(args.jobs, args.suite).items():
        if any(s in name for s in skip):
            continue
        results[name] = []
        for i in range(args.repeat):
            r = run_once(cmd)
            results[name].append(r)
            print(f"{name} run{i}: {r['wall_s']}s exit={r['exit']} {r['tail'][:80]}")

    summary = {
        name: {
            "best_s": min(r["wall_s"] for r in runs),
            "mean_s": round(sum(r["wall_s"] for r in runs) / len(runs), 4),
            "exit": runs[0]["exit"],
        }
        for name, runs in results.items()
    }
    doc = {
        "suite": args.suite,
        "jobs": args.jobs,
        "cpu_count": os.cpu_count(),
        "summary": summary,
        "raw": results,
    }
    print(json.dumps(summary, indent=2))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(doc, indent=2))


if __name__ == "__main__":
    main()
