"""Phase 2: diff -> affected test selection, with receipts.

Given the map.sqlite from Phase 1 and a git diff, compute:
  - changed (file, qualname) functions via unified-diff hunks + AST spans
  - affected tests = inverted map lookup
  - conservative set = unmapped tests, tests in changed test files,
    everything if conftest / non-Python / module-level code changed

Usage: python -m fastest.select --repo testbeds/httpx --base HEAD~1 [--json]
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sqlite3
import subprocess
from pathlib import Path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# files that cannot affect test outcomes: skip entirely
INERT_SUFFIXES = (".md", ".rst", ".txt", ".lock")
INERT_PREFIXES = ("docs/", ".github/", ".gitignore")
INERT_NAMES = {"LICENSE", "LICENSE.md", "CHANGELOG.md", "README.md", "CODE_OF_CONDUCT.md"}


def is_inert(path: str) -> bool:
    return (
        path.endswith(INERT_SUFFIXES)
        or path.startswith(INERT_PREFIXES)
        or Path(path).name in INERT_NAMES
    )


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def function_spans(source: str) -> list[tuple[str, int, int]]:
    """[(qualname, start_lineno_incl_decorators, end_lineno)] for all functions."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    spans: list[tuple[str, int, int]] = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                spans.append((qual, start, child.end_lineno or child.lineno))
                walk(child, f"{qual}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return spans


def changed_lines(repo: Path, base: str, head: str | None) -> dict[str, dict]:
    """path -> {'new': set[int], 'old': set[int], 'status': 'M'/'A'/'D'/...}"""
    rng = [base, head] if head else [base]
    out: dict[str, dict] = {}
    status = git(repo, "diff", "--name-status", "-M", *rng)
    for line in status.splitlines():
        parts = line.split("\t")
        st, path = parts[0][0], parts[-1]
        out[path] = {"new": set(), "old": set(), "status": st}
    diff = git(repo, "diff", "-U0", "-M", *rng)
    cur = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
        elif line.startswith("@@") and cur in out:
            m = HUNK_RE.match(line)
            if m:
                a, b = int(m.group(1)), int(m.group(2) or "1")
                c, d = int(m.group(3)), int(m.group(4) or "1")
                out[cur]["old"].update(range(a, a + b))
                out[cur]["new"].update(range(c, c + d))
    return out


def funcs_touching(spans, lines) -> tuple[set[str], set[int]]:
    """(qualnames whose span overlaps lines, lines outside any function)."""
    hit, covered = set(), set()
    for qual, s, e in spans:
        overlap = {ln for ln in lines if s <= ln <= e}
        if overlap:
            hit.add(qual)
        covered |= overlap
    return hit, lines - covered


def collection_deps(con) -> tuple[set[tuple[str, str]], set[str]]:
    """(funcs, files) executed at import/collection time."""
    rows = con.execute(
        "SELECT f.path, fn.qualname FROM tests t JOIN links l ON l.test_id=t.id "
        "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
        "WHERE t.test_id='__collection__'"
    ).fetchall()
    return {(p, q) for p, q in rows}, {p for p, _ in rows}


def select(db_path: Path, repo: Path, base: str, head: str | None = None) -> dict:
    con = sqlite3.connect(db_path)
    all_tests = {
        r[0]: r[1]
        for r in con.execute("SELECT test_id, mapped FROM tests")
        if r[0] != "__collection__"
    }
    coll_funcs, coll_files = collection_deps(con)

    changes = changed_lines(repo, base, head)
    selected: dict[str, str] = {}  # test_id -> reason
    run_all_reasons: list[str] = []
    changed_funcs: set[tuple[str, str]] = set()  # (path, qualname)
    changed_files_wholesale: set[str] = set()

    for path, ch in changes.items():
        name = Path(path).name
        if is_inert(path):
            continue
        if not path.endswith(".py"):
            run_all_reasons.append(f"non-Python file changed: {path}")
            continue
        if name == "conftest.py" or "conftest" in name:
            run_all_reasons.append(f"conftest changed: {path}")
            continue
        if ch["status"] == "A":
            # brand-new file: nothing maps to it; select tests defined in it
            if name.startswith("test_") or name.endswith("_test.py"):
                for t in all_tests:
                    if t.startswith(path + "::"):
                        selected[t] = f"new test file {path}"
            else:
                changed_files_wholesale.add(path)
            continue
        if ch["status"] == "D":
            changed_files_wholesale.add(path)
            continue

        # modified file: map changed lines -> functions, in NEW and OLD version
        module_level = set()
        try:
            new_src = (repo / path).read_text()
            hit, uncovered = funcs_touching(function_spans(new_src), ch["new"])
            changed_funcs |= {(path, q) for q in hit}
            module_level |= uncovered
        except FileNotFoundError:
            changed_files_wholesale.add(path)
        try:
            old_src = git(repo, "show", f"{base}:{path}")
            hit, uncovered = funcs_touching(function_spans(old_src), ch["old"])
            changed_funcs |= {(path, q) for q in hit}
            module_level |= uncovered
        except subprocess.CalledProcessError:
            pass
        if module_level:
            # import-time code changed: every test importing this file is suspect
            changed_files_wholesale.add(path)

        if name.startswith("test_") or name.endswith("_test.py"):
            for t in all_tests:
                if t.startswith(path + "::"):
                    selected.setdefault(t, f"test file changed: {path}")

    # import/collection-time execution: a changed function that runs during
    # collection (decorators, module-level declarations) can break any test
    # before it even starts — no per-test attribution possible, so run all.
    for path, qual in sorted(changed_funcs):
        if (path, qual) in coll_funcs:
            run_all_reasons.append(f"executed at import/collection time: {path}::{qual}")
    for path in sorted(changed_files_wholesale):
        if path in coll_files:
            run_all_reasons.append(f"module-level change in import-time file: {path}")

    if run_all_reasons:
        con.close()
        return {
            "mode": "run_all",
            "reasons": run_all_reasons,
            "selected": sorted(all_tests),
            "n_total": len(all_tests),
        }

    # inverted-map lookup: function-level
    for path, qual in changed_funcs:
        rows = con.execute(
            "SELECT t.test_id FROM tests t JOIN links l ON l.test_id=t.id "
            "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
            "WHERE f.path=? AND fn.qualname=?",
            (path, qual),
        )
        for (t,) in rows:
            selected.setdefault(t, f"touches changed function {path}::{qual}")

    # file-level (wholesale) lookup
    for path in changed_files_wholesale:
        rows = con.execute(
            "SELECT DISTINCT t.test_id FROM tests t JOIN links l ON l.test_id=t.id "
            "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
            "WHERE f.path=?",
            (path,),
        )
        for (t,) in rows:
            selected.setdefault(t, f"imports/touches changed file {path}")

    # conservative: unmapped tests always run
    for t, mapped in all_tests.items():
        if not mapped:
            selected.setdefault(t, "unmapped test (conservative)")

    skipped = {
        t: "no overlap between this test's coverage map and the diff"
        for t in all_tests
        if t not in selected
    }
    con.close()
    return {
        "mode": "select",
        "changed_functions": sorted(f"{p}::{q}" for p, q in changed_funcs),
        "changed_files_wholesale": sorted(changed_files_wholesale),
        "selected": {t: selected[t] for t in sorted(selected)},
        "n_selected": len(selected),
        "n_skipped": len(skipped),
        "n_total": len(all_tests),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--head", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    db = args.db or args.repo / ".fastest" / "map.sqlite"
    result = select(db, args.repo.resolve(), args.base, args.head)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if result["mode"] == "run_all":
            print(f"RUN ALL ({result['n_total']} tests): {'; '.join(result['reasons'])}")
        else:
            print(
                f"selected {result['n_selected']}/{result['n_total']} "
                f"({result['n_skipped']} skipped)"
            )
            for f in result["changed_functions"]:
                print(f"  changed: {f}")
            for t, why in list(result["selected"].items())[:20]:
                print(f"  RUN  {t}  [{why}]")


if __name__ == "__main__":
    main()
