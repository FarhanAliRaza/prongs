#!/usr/bin/env python3
"""Phase 0 benchmark harness: the before-picture, per testbed repo.

Usage: python bench/baseline.py testbeds/httpx [--single tests/test_x.py::test_y]

Records into results/<repo>.json:
  - collect_s   : cold `pytest --collect-only -q` wall time + test count
  - full_run_s  : cold full-suite wall time
  - single_s    : cold single-test wall time (default: first collected test)
  - cov_run_s   : full-suite wall time with --fastest-cov (Phase 1 overhead)

Each pytest invocation is a fresh interpreter (cold), run REPS times, min taken.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPS = 3


def run(cmd: list[str], cwd: Path, env=None) -> tuple[float, subprocess.CompletedProcess]:
    t0 = time.monotonic()
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    return time.monotonic() - t0, p


def timed(cmd: list[str], cwd: Path, reps: int = REPS) -> dict:
    times, last = [], None
    for _ in range(reps):
        dt, last = run(cmd, cwd)
        times.append(round(dt, 3))
    return {
        "min_s": min(times),
        "median_s": round(statistics.median(times), 3),
        "times": times,
        "exit": last.returncode,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--single", help="node id for the single-test measurement")
    ap.add_argument("--pytest-args", default="", help="extra args for the full run")
    ap.add_argument("--skip-full", action="store_true")
    args = ap.parse_args()

    repo = args.repo.resolve()
    py = repo / ".venv" / "bin" / "python"
    assert py.exists(), f"no venv at {py}"
    pytest_cmd = [str(py), "-m", "pytest"]
    extra = args.pytest_args.split() if args.pytest_args else []
    out: dict = {"repo": repo.name, "python": str(py)}

    # 1. cold collection
    print(f"[{repo.name}] collect-only x{REPS} ...", flush=True)
    out["collect"] = timed(pytest_cmd + ["--collect-only", "-q"] + extra, repo)
    _, p = run(pytest_cmd + ["--collect-only", "-q"] + extra, repo)
    m = re.search(r"(\d+) tests? collected", p.stdout)
    out["n_tests"] = int(m.group(1)) if m else None
    print(f"  {out['collect']['min_s']}s, {out['n_tests']} tests")

    # 2. single test, cold
    single = args.single
    if not single:
        for line in p.stdout.splitlines():
            if "::" in line and not line.startswith(" "):
                single = line.strip()
                break
    if single:
        print(f"[{repo.name}] single test ({single}) x{REPS} ...", flush=True)
        out["single"] = {"node": single, **timed(pytest_cmd + [single] + extra, repo)}
        print(f"  {out['single']['min_s']}s")

    # 3. full run, cold (once — long)
    if not args.skip_full:
        print(f"[{repo.name}] full run ...", flush=True)
        dt, p2 = run(pytest_cmd + ["-q"] + extra, repo)
        out["full_run"] = {"s": round(dt, 3), "exit": p2.returncode,
                           "tail": p2.stdout.splitlines()[-1] if p2.stdout else ""}
        print(f"  {out['full_run']['s']}s  ({out['full_run']['tail']})")

        # 4. full run with coverage map (Phase 1 overhead)
        print(f"[{repo.name}] full run + fastest-cov ...", flush=True)
        dt, p3 = run(pytest_cmd + ["-q", "--fastest-cov"] + extra, repo)
        out["cov_run"] = {"s": round(dt, 3), "exit": p3.returncode,
                          "stderr_tail": p3.stderr.strip().splitlines()[-1] if p3.stderr.strip() else ""}
        base = out["full_run"]["s"]
        ovh = (dt - base) / base * 100 if base else 0
        out["cov_overhead_pct"] = round(ovh, 1)
        print(f"  {out['cov_run']['s']}s  -> overhead {out['cov_overhead_pct']}%")

    res = Path(__file__).parent.parent / "results" / f"{repo.name}.json"
    res.write_text(json.dumps(out, indent=2))
    print(f"wrote {res}")


if __name__ == "__main__":
    main()
