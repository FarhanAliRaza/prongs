#!/usr/bin/env python3
"""Phase 2 strong validation: fault injection.

For N randomly chosen project functions (that appear in the coverage map):
  1. insert `raise RuntimeError("fastest-mutation")` as the first statement
  2. compute the selected test set for the working-tree diff (map@HEAD)
  3. run the FULL suite; every newly-failing test must be in the selected set
  4. revert

MISS = a test that failed under mutation but was not selected. Each miss is
re-run alone under the same mutation: if it still fails it is a first-order
miss (the selector's fault); if it passes alone it is second-order pollution
from an already-selected failure (a broken teardown's unraisable exception
landing on whichever test runs next, leaked state, ...) — reported
separately, because fork-per-test isolation, not selection, is the fix.
Kill criterion: any first-order miss.

Usage: python bench/mutate.py testbeds/httpx --n 15 [--seed 7]
       python bench/mutate.py testbeds/httpx --target httpx/_client.py::Client.__exit__
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from fastest.select import select, function_spans  # noqa: E402


def run_statuses(py: Path, repo: Path, extra: list[str], tag: str) -> dict[str, str]:
    """Full run; per-test statuses via our own plugin's SQLite (no stdout parsing).

    Tests absent from the result db (e.g. their module ERRORed at collection)
    are simply missing — the caller treats disappearance as a status change.
    """
    db = Path(f"/tmp/claude-1000/fastest-mutate-{repo.name}-{os.getpid()}-{tag}.sqlite")
    db.unlink(missing_ok=True)
    env = dict(os.environ, FASTEST_COV="1", FASTEST_DB=str(db))
    subprocess.run(
        [str(py), "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "--tb=no", "--continue-on-collection-errors"] + extra,
        cwd=repo, capture_output=True, text=True, env=env,
    )
    if not db.exists():
        # pytest died before writing results (e.g. conftest/package import
        # broken): treat as "no test survived" — every test changed status.
        return {}
    con = sqlite3.connect(db)
    out = {
        t: s for t, s in con.execute("SELECT test_id, status FROM tests")
        if t != "__collection__"
    }
    con.close()
    return out


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", type=Path)
    ap.add_argument("--n", type=int, default=15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--pytest-args", default="")
    ap.add_argument("--target", action="append", default=[], metavar="PATH::QUALNAME",
                    help="mutate exactly this function (repeatable) instead of sampling")
    args = ap.parse_args()
    repo = args.repo.resolve()
    py = repo / ".venv" / "bin" / "python"
    extra = args.pytest_args.split() if args.pytest_args else []
    db = repo / ".fastest" / "map.sqlite"
    assert db.exists(), "build the map first (pytest --fastest-cov)"

    # candidate functions: mapped project functions outside tests/
    con = sqlite3.connect(db)
    rows = con.execute(
        "SELECT DISTINCT f.path, fn.qualname FROM funcs fn JOIN files f ON f.id=fn.file_id "
        "JOIN current_links l ON l.func_id=fn.id WHERE f.path NOT LIKE 'tests%' "
        "AND f.path NOT LIKE '%conftest%'"
    ).fetchall()
    con.close()
    if args.target:
        rows = [tuple(t.split("::", 1)) for t in args.target]
        args.n = len(rows)
    else:
        rng = random.Random(args.seed)
        rng.shuffle(rows)

    baseline = run_statuses(py, repo, extra, "base")
    print(f"baseline: {len(baseline)} tests, "
          f"{sum(1 for s in baseline.values() if s == 'failed')} failing")

    results, tried = [], 0
    for path, qual in rows:
        if tried >= args.n:
            break
        if "<locals>" in qual:  # nested funcs: mutation site ambiguous, skip
            continue
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo)
        if not mutate_function(repo, path, qual):
            continue
        tried += 1
        t0 = time.monotonic()
        sel = select(db, repo, "HEAD")
        sel_set = set(sel["selected"])
        cur = run_statuses(py, repo, extra, "mut")
        # status change = flipped outcome, or vanished (collection error)
        new_fails = {
            t for t, s in baseline.items()
            if cur.get(t, "absent") != s and s != "failed"
        }
        candidates = sorted(new_fails - sel_set) if sel["mode"] == "select" else []
        # classify: does the miss reproduce alone, under the same mutation?
        misses, pollution = [], []
        for t in candidates:
            alone = run_statuses(py, repo, extra + [t], "alone")
            (misses if alone.get(t, "absent") != baseline[t] else pollution).append(t)
        dt = time.monotonic() - t0
        results.append({
            "target": f"{path}::{qual}",
            "mode": sel["mode"],
            "n_selected": len(sel_set),
            "n_new_failures": len(new_fails),
            "misses": misses,
            "pollution": pollution,
        })
        flag = "  !!! MISS !!!" if misses else ("  (pollution only)" if pollution else "")
        print(
            f"[{tried}/{args.n}] {path}::{qual}: mode={sel['mode']} "
            f"selected {len(sel_set)}, status-changes {len(new_fails)}, "
            f"misses {len(misses)}, pollution {len(pollution)}{flag}  ({dt:.0f}s)",
            flush=True,
        )
        for m in misses:
            print(f"      MISSED (fails alone): {m}")
        for m in pollution:
            print(f"      polluted (passes alone): {m}")
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo)

    out = Path(__file__).parent.parent / "results" / f"mutate-{repo.name}.json"
    out.write_text(json.dumps(results, indent=2))
    total_misses = sum(len(r["misses"]) for r in results)
    total_pollution = sum(len(r["pollution"]) for r in results)
    killed = sum(1 for r in results if r["n_new_failures"] > 0)
    print(f"\n=== {len(results)} mutations, {killed} caused failures, "
          f"first-order MISSES: {total_misses} "
          f"{'<<< KILL CRITERION HIT' if total_misses else '(zero — pass)'}, "
          f"second-order pollution: {total_pollution}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
