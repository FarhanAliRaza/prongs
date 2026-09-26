#!/usr/bin/env python3
"""Phase 2 strong validation: fault injection, as a loop around `fastest audit`.

For N randomly chosen project functions (that appear in the coverage map):
  1. insert `raise RuntimeError("fastest-mutation")` as the first statement
  2. audit --base HEAD: select for the working-tree diff against map@HEAD,
     run the FULL suite, diff every test's status against the map, and re-run
     each unselected status change alone under the same mutation
  3. revert

audit() splits the unselected changes: a first-order miss still differs when
run alone (the selector's fault — the kill criterion); second-order pollution
passes alone (fallout from an already-selected failure, e.g. a broken
teardown's unraisable exception landing on whichever test runs next), which
fork-per-test isolation fixes, not selection.

The map must have been built at HEAD on a clean tree (pytest --fastest-cov):
its recorded statuses are the baseline.

Usage: python bench/mutate.py testbeds/httpx --n 15 [--seed 7]
       python bench/mutate.py testbeds/httpx --target httpx/_client.py::Client.__exit__
"""

from __future__ import annotations

import argparse
import ast
import json
import random
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from fastest import mapdb, provenance  # noqa: E402
from fastest.audit import audit  # noqa: E402


def mutate_function(repo: Path, path: str, qual: str) -> bool:
    """Insert a raise as the first body statement of function `qual` in file."""
    src = (repo / path).read_text()
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    target = None

    def walk(node, prefix):
        nonlocal target
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{child.name}"
                if q == qual:
                    target = child
                walk(child, f"{q}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    if target is None or not target.body:
        return False
    first = target.body[0]
    # skip docstring so the raise is live code
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and len(target.body) > 1
    ):
        first = target.body[1]
    indent = " " * first.col_offset
    lines.insert(first.lineno - 1, f'{indent}raise RuntimeError("fastest-mutation")\n')
    (repo / path).write_text("".join(lines))
    return True


def mapped_functions(db: Path) -> list[tuple[str, str]]:
    """Candidate functions: mapped project functions outside tests/."""
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT DISTINCT f.path, fn.qualname FROM funcs fn JOIN files f ON f.id=fn.file_id "
            "JOIN current_links l ON l.func_id=fn.id WHERE f.path NOT LIKE 'tests%' "
            "AND f.path NOT LIKE '%conftest%' ORDER BY f.path, fn.qualname"
        ).fetchall()
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--pytest-args", default="")
    ap.add_argument("--target", action="append", default=[], metavar="PATH::QUALNAME",
                    help="mutate exactly this function (repeatable) instead of sampling")
    ap.add_argument("--out", type=Path, help="results JSON (default: results/mutate-<repo>.json)")
    args = ap.parse_args()
    repo = args.repo.resolve()
    py = str(repo / ".venv" / "bin" / "python")
    extra = shlex.split(args.pytest_args)
    db = repo / ".fastest" / "map.sqlite"
    assert db.exists(), "build the map first (pytest --fastest-cov)"
    if subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=repo).returncode != 0:
        sys.exit(f"{repo} has uncommitted changes; mutate needs a clean tree (git stash)")
    con = mapdb.connect(str(db))
    full = mapdb.last_full_run(con)
    con.close()
    if not full or full["commit_sha"] != provenance.git_head(repo):
        print("warning: the map's last full run is not at HEAD; its statuses are the baseline")

    if args.target:
        rows = [tuple(t.split("::", 1)) for t in args.target]
        args.n = len(rows)
    else:
        rows = mapped_functions(db)
        random.Random(args.seed).shuffle(rows)

    results, tried = [], 0
    try:
        for path, qual in rows:
            if tried >= args.n:
                break
            if "<locals>" in qual:  # nested funcs: mutation site ambiguous, skip
                continue
            subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo)
            if not mutate_function(repo, path, qual):
                continue
            tried += 1
            res = audit(repo, "HEAD", python=py, pytest_args=extra)
            if res.get("verdict") == "error":
                print(f"[{tried}/{args.n}] {path}::{qual}: audit error: {res.get('error')}")
                results.append({"target": f"{path}::{qual}", "error": res.get("error")})
                continue
            rec = {
                "target": f"{path}::{qual}",
                "mode": res["selection"]["mode"],
                "n_selected": res["selection"]["n_selected"],
                "n_status_changes": res["status_changes"]["total"],
                "misses": [m["test"] for m in res["misses"]],
                "pollution": [p["test"] for p in res["pollution"]],
                "miss_receipts": res["misses"],
                "wall_s": res["wall_s"],
            }
            results.append(rec)
            flag = ("  !!! MISS !!!" if rec["misses"]
                    else "  (pollution only)" if rec["pollution"] else "")
            print(
                f"[{tried}/{args.n}] {path}::{qual}: mode={rec['mode']} "
                f"selected {rec['n_selected']}, status-changes {rec['n_status_changes']}, "
                f"misses {len(rec['misses'])}, pollution {len(rec['pollution'])}{flag}"
                f"  ({rec['wall_s']:.0f}s)",
                flush=True,
            )
            for m in rec["misses"]:
                print(f"      MISSED (fails alone): {m}")
            for m in rec["pollution"]:
                print(f"      polluted (passes alone): {m}")
    finally:
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo)

    out = args.out or Path(__file__).parent.parent / "results" / f"mutate-{repo.name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    ok = [r for r in results if "error" not in r]
    total_misses = sum(len(r["misses"]) for r in ok)
    total_pollution = sum(len(r["pollution"]) for r in ok)
    killed = sum(1 for r in ok if r["n_status_changes"] > 0)
    print(f"\n=== {len(results)} mutations ({len(results) - len(ok)} audit errors), "
          f"{killed} caused failures, first-order MISSES: {total_misses} "
          f"{'<<< KILL CRITERION HIT' if total_misses else '(zero — pass)'}, "
          f"second-order pollution: {total_pollution}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
