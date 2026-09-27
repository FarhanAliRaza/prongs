"""Phase 2: diff -> affected test selection, with receipts and evidence.

Reads the roll-up in map.sqlite (tests + current_links) and never writes:
journal files reach it only through `fastest rollup`. A test's evidence is
the tree of the run whose coverage it last recorded (`deps_run`), so
selection is computed per observed tree:

  1. group the contributing runs (last full run and everything after it, plus
     any older run that still holds a live test's last observation) by the
     tree they saw: HEAD commit + the files that were dirty, by content hash
  2. per tree, compute what changed between that tree and now: the git diff
     from its commit, with each dirty file's git hunks replaced by an exact
     line diff of the content the run saw (stored in the journal) against the
     content now — so a file that is still what the run saw is not a change,
     and a file that differs is diffed at function level, not wholesale
  3. map changed lines to (file, qualname) functions via AST spans, look them
     up in the inverted map, restricted to the tests that tree observed
  4. history on top (the second signal): a test whose outcome flipped in the
     last N rollups is selected ("recent status change"), and a test that
     both passed and failed on one tree is reported as flaky
  5. the map-staleness safety net: a changed function the map has never seen
     selects every test that touches its file; a changed file it has never
     seen, and that did not exist when the evidence was recorded, runs
     everything; so does a map more than max_map_age commits behind HEAD
  6. conservative rules on top: unmapped tests, tests in changed test files
     (a test module whose known tests can't all be found statically, or that
     has tests the map has never seen, is run as a whole file), and run-all
     when conftest / a tracked non-Python file / module-level or import-time
     code of a production module changed in any tree. Untracked non-Python
     files are ignored.

An explicit --base collapses this to one tree (that commit, all tests), which
is what the replay/mutation harnesses use. Every result carries `evidence`:
runs, commits, freshness, the trees, and every warning.

Usage: python -m fastest.select --repo testbeds/httpx [--base REV] [--json]
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastest import config, journal, mapdb, provenance

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


def is_test_file(path: str) -> bool:
    name = Path(path).name
    return name.startswith("test_") or name.endswith("_test.py")


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


def static_test_names(source: str) -> set[str]:
    """Test names pytest would collect from a module, statically: 'test_x' and
    'TestC::test_y' (parametrization ignored). Used to spot tests the map has
    never seen; canonical ids still come from pytest."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    def base_name(b) -> str:
        return b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else ""

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            names.add(node.name)
        elif isinstance(node, ast.ClassDef) and (
            node.name.startswith("Test")
            or any(base_name(b).endswith("TestCase") for b in node.bases)  # unittest style
        ):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    names.add(f"{node.name}::{sub.name}")
    return names


def parse_hunks(lines) -> tuple[set[int], set[int]]:
    """(old_lines, new_lines) from unified-diff hunk headers (-U0 style)."""
    old: set[int] = set()
    new: set[int] = set()
    for line in lines:
        if line.startswith("@@"):
            m = HUNK_RE.match(line)
            if m:
                a, b = int(m.group(1)), int(m.group(2) or "1")
                c, d = int(m.group(3)), int(m.group(4) or "1")
                old.update(range(a, a + b))
                new.update(range(c, c + d))
    return old, new


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
    chunk: list[str] = []

    def flush():
        if cur in out:
            o, n = parse_hunks(chunk)
            out[cur]["old"].update(o)
            out[cur]["new"].update(n)

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            flush()
            cur, chunk = line[6:], []
        elif line.startswith("@@"):
            chunk.append(line)
    flush()
    return out


def local_diff(old_text: str, new_text: str) -> tuple[set[int], set[int]]:
    """(old_lines, new_lines) that differ, same convention as git diff -U0."""
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), n=0
    )
    return parse_hunks(diff)


def funcs_touching(spans, lines, source: str | None = None) -> tuple[set[str], set[int]]:
    """(qualnames whose span overlaps lines, lines outside any function).
    With `source`, blank and comment-only lines outside functions are ignored:
    they cannot execute, so they are not module-level changes."""
    hit, covered = set(), set()
    for qual, s, e in spans:
        overlap = {ln for ln in lines if s <= ln <= e}
        if overlap:
            hit.add(qual)
        covered |= overlap
    outside = lines - covered
    if source is not None and outside:
        src_lines = source.splitlines()

        def inert(ln: int) -> bool:
            text = src_lines[ln - 1].strip() if 0 < ln <= len(src_lines) else ""
            return text == "" or text.startswith("#")

        outside = {ln for ln in outside if not inert(ln)}
    return hit, outside


def collection_deps(con) -> tuple[set[tuple[str, str]], set[str]]:
    """(funcs, files) executed at import/collection time."""
    rows = con.execute(
        "SELECT f.path, fn.qualname FROM tests t JOIN current_links l ON l.test_id=t.id "
        "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
        "WHERE t.test_id=?",
        (mapdb.COLLECTION,),
    ).fetchall()
    return {(p, q) for p, q in rows}, {p for p, _ in rows}


# --- evidence ----------------------------------------------------------------

@dataclass
class Tree:
    """One observed state of the repository."""
    commit: str                     # commit (or rev) to diff from
    dirty: dict[str, str | None]    # path -> content hash the runs saw (None: absent)
    unknown: set[str]               # paths that changed during an observing run
    run_ids: set[int] | None        # runs that saw this tree; None = every test

    def summary(self) -> dict:
        return {
            "commit": self.commit if len(self.commit) != 40 else self.commit[:8],
            "dirty": sorted(self.dirty),
            "unknown": sorted(self.unknown),
            "runs": sorted(self.run_ids) if self.run_ids is not None else "all",
        }


@dataclass
class Analysis:
    """What changed between one tree and now, and what that implies."""
    changed_funcs: set[tuple[str, str]] = field(default_factory=set)
    wholesale: set[str] = field(default_factory=set)
    run_all_reasons: list[str] = field(default_factory=list)
    test_files: dict[str, str] = field(default_factory=dict)      # changed test file -> reason
    selected_files: dict[str, str] = field(default_factory=dict)  # run whole file -> reason
    vanished: set[str] = field(default_factory=set)  # known tests no longer found statically
    production: set[str] = field(default_factory=set)  # changed, non-test .py files (not deleted)
    unseen: dict[str, list[str]] = field(default_factory=dict)  # file -> changed funcs the map never saw
    unchanged: list[str] = field(default_factory=list)
    forced: list[str] = field(default_factory=list)


def _brief(run: dict | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run["id"],
        "scope": run["scope"],
        "mode": run["mode"],
        "commit": run["commit_sha"],
        "finished_at": run["finished_at"],
        "age_s": round(time.time() - run["finished_at"], 1) if run["finished_at"] else None,
        "n_observed": run["n_observed"],
        "n_collected": run["n_collected"],
        "dirty_files": len(run["dirty_files"]),
        "tree_changed": run["tree_changed"],
        "recorder": run["recorder"],
    }


def _rollup_meta(con) -> dict:
    """Where the last `fastest rollup` left the map."""
    m = mapdb.meta(con)
    return {
        "seq": int(m["rollup_seq"]) if m.get("rollup_seq") else None,
        "run": int(m["rollup_run"]) if m.get("rollup_run") else None,
        "commit": m.get("rollup_commit") or None,
    }


def evidence_summary(con, repo: Path) -> tuple[dict, list[dict]]:
    """(evidence block, contributing runs)."""
    all_runs = mapdb.runs(con, lineage="own")
    full = mapdb.last_full_run(con)
    contributing = mapdb.contributing_runs(con)
    commits = sorted({r["commit_sha"] for r in contributing if r["commit_sha"]})
    refreshed = None
    if full:
        refreshed = con.execute(
            "SELECT COUNT(*) FROM tests t JOIN runs d ON d.id = t.deps_run "
            "WHERE t.retired_run IS NULL AND t.test_id != ? "
            "AND (COALESCE(d.finished_at, 0), d.id) > (?, ?)",
            (mapdb.COLLECTION, full["finished_at"] or 0.0, full["id"]),
        ).fetchone()[0]
    ev = {
        "schema": mapdb.SCHEMA_VERSION,
        "runs": len(all_runs),
        "foreign_runs": con.execute(
            "SELECT COUNT(*) FROM runs WHERE lineage='foreign'").fetchone()[0],
        "last_full_run": _brief(full),
        "last_run": _brief(all_runs[-1]) if all_runs else None,
        "contributing_runs": [r["id"] for r in contributing],
        "evidence_commits": commits,
        "head": provenance.git_head(repo),
        "commits_behind": (
            provenance.commits_behind(repo, full["commit_sha"])
            if full and full["commit_sha"] else None
        ),
        "tests_refreshed_since_full": refreshed,
        "rollup": _rollup_meta(con),
        "warnings": [],
    }
    if not full:
        ev["warnings"].append("no full run recorded: evidence is partial runs only")
    return ev, contributing


def build_trees(repo: Path, contributing: list[dict], base: str | None, ev: dict,
                missing: dict[int, str] | None = None) -> list[Tree]:
    """The trees the evidence was observed on. A run whose commit this
    repository does not have (a deleted branch, a shallow clone) has no tree
    to diff from: it goes into `missing` (run id -> commit) and its tests are
    selected, never diffed from HEAD — which would show none of the changes
    since and skip them."""
    if base is not None:
        ev["base_source"] = "explicit"
        sha = provenance.rev_parse(repo, base)
        if ev["evidence_commits"] and sha not in ev["evidence_commits"]:
            ev["warnings"].append(
                f"explicit base {base} is not an evidence commit "
                f"{[c[:8] for c in ev['evidence_commits']]}; differences between them are invisible"
            )
        return [Tree(commit=base, dirty={}, unknown=set(), run_ids=None)]
    ev["base_source"] = "evidence"
    exists: dict[str, bool] = {}
    groups: dict[tuple, set[int]] = {}
    for r in contributing:
        commit = r["commit_sha"]
        if commit is not None:
            if commit not in exists:
                exists[commit] = provenance.rev_parse(repo, commit) is not None
            if not exists[commit]:
                ev["warnings"].append(
                    f"run #{r['id']}: evidence commit {commit[:8]} is not in this repository"
                    f"{provenance.shallow_hint(repo)}; its tests are selected"
                )
                if missing is not None:
                    missing[r["id"]] = commit
                continue
        else:
            ev["warnings"].append(f"run #{r['id']} carries no commit; diffing from HEAD")
        commit = commit or "HEAD"
        dirty = {p: he for p, (hs, he) in r["dirty_files"].items() if hs == he}
        unknown = {p for p, (hs, he) in r["dirty_files"].items() if hs != he}
        if unknown:
            ev["warnings"].append(
                f"run #{r['id']}: {sorted(unknown)} changed while it ran; selected wholesale"
            )
        key = (commit, tuple(sorted(dirty.items())), tuple(sorted(unknown)))
        groups.setdefault(key, set()).add(r["id"])
    return [
        Tree(commit=c, dirty=dict(d), unknown=set(u), run_ids=ids)
        for (c, d, u), ids in sorted(groups.items(), key=lambda kv: min(kv[1]))
    ]


class _Now:
    """Current content of paths, in the working tree or at `head`, cached."""

    def __init__(self, repo: Path, head: str | None):
        self.repo, self.head = repo, head
        self._cache: dict[str, tuple[str | None, bytes | None]] = {}

    def get(self, path: str) -> tuple[str | None, bytes | None]:
        if path not in self._cache:
            if self.head is None:
                self._cache[path] = provenance.read_and_hash(self.repo / path)
            else:
                try:
                    data = subprocess.run(
                        ["git", "show", f"{self.head}:{path}"], cwd=self.repo,
                        capture_output=True, check=True,
                    ).stdout
                except subprocess.CalledProcessError:
                    self._cache[path] = (None, None)
                else:
                    self._cache[path] = (provenance.bytes_hash(data), data)
        return self._cache[path]

    def text(self, path: str) -> str | None:
        data = self.get(path)[1]
        if data is None:
            return None
        try:
            return data.decode()
        except UnicodeDecodeError:
            return None


def tree_changes(
    repo: Path, tree: Tree, now: _Now, con, git_cache: dict, untracked: dict[str, str | None],
    an: Analysis,
) -> dict[str, dict]:
    """path -> {'new': set, 'old': {source: set}, 'status', ['wholesale']}
    between the tree and now. `source` is a rev for git-derived hunks or
    'blob:<hash>' for hunks against content the run saw."""
    if tree.commit not in git_cache:
        git_cache[tree.commit] = changed_lines(repo, tree.commit, now.head)
    changes = {
        path: {"new": set(ch["new"]), "old": {tree.commit: set(ch["old"])}, "status": ch["status"]}
        for path, ch in git_cache[tree.commit].items()
    }
    for path, h in untracked.items():  # never in git diff; new to this tree unless it saw them
        if path not in changes and path not in tree.dirty and path.endswith(".py") and h is not None:
            changes[path] = {"new": set(), "old": {}, "status": "A"}
    for path, observed_hash in tree.dirty.items():
        now_hash, _ = now.get(path)
        if observed_hash == now_hash:
            changes.pop(path, None)
            an.unchanged.append(path)
            continue
        if not path.endswith(".py"):
            # same policy as the git path: a tracked non-Python change is
            # already in `changes` (run-all); an untracked one is ignored
            continue
        if observed_hash is None:  # absent when observed
            if now_hash is not None:
                changes[path] = {"new": set(), "old": {}, "status": "A"}
            else:
                changes.pop(path, None)
            continue
        if now_hash is None:
            changes[path] = {"new": set(), "old": {}, "status": "D"}
            continue
        observed = mapdb.blob(con, observed_hash)
        new_text = now.text(path)
        try:
            old_text = observed.decode() if observed is not None else None
        except UnicodeDecodeError:
            old_text = None
        if old_text is None or new_text is None:
            changes[path] = {
                "new": set(), "old": {}, "status": "M",
                "wholesale": "dirty at observation; observed content unavailable",
            }
            an.forced.append(path)
            continue
        old_lines, new_lines = local_diff(old_text, new_text)
        changes[path] = {
            "new": new_lines, "old": {f"blob:{observed_hash}": old_lines}, "status": "M",
        }
    for path in tree.unknown:
        if not path.endswith(".py"):
            continue
        now_hash, _ = now.get(path)
        changes[path] = {
            "new": set(), "old": {}, "status": "M" if now_hash is not None else "D",
            "wholesale": "changed while the observing run was executing",
        }
        an.forced.append(path)
    return changes


def analyze(repo: Path, con, now: _Now, changes: dict[str, dict], all_tests: dict, an: Analysis) -> Analysis:
    """Turn a change set into changed functions, wholesale files, run-all
    reasons and test-file selections."""

    def old_source(source: str, path: str) -> str | None:
        if source.startswith("blob:"):
            data = mapdb.blob(con, source[5:])
            try:
                return data.decode() if data is not None else None
            except UnicodeDecodeError:
                return None
        try:
            return git(repo, "show", f"{source}:{path}")
        except subprocess.CalledProcessError:
            return None

    def select_test_file(path: str, reason: str) -> None:
        an.test_files[path] = reason
        src = now.text(path)
        if src is None:
            return
        known = {
            t.split("::", 1)[1].split("[", 1)[0] for t in all_tests if t.startswith(path + "::")
        }
        names = static_test_names(src)
        unknown = sorted(names - known)
        vanished = sorted(known - names)
        notes = []
        if unknown:
            notes.append(f"{len(unknown)} test(s) not in the map: {', '.join(unknown[:5])}"
                         + (" ..." if len(unknown) > 5 else ""))
        if vanished:
            # deleted, renamed, or generated dynamically: their node ids can't
            # be trusted, so run the file and let pytest decide what exists
            notes.append(f"{len(vanished)} known test(s) not found statically: "
                         f"{', '.join(vanished[:5])}" + (" ..." if len(vanished) > 5 else ""))
            gone = set(vanished)
            an.vanished |= {
                t for t in all_tests
                if t.startswith(path + "::") and t.split("::", 1)[1].split("[", 1)[0] in gone
            }
        if notes:
            an.selected_files[path] = "; ".join(notes)

    for path, ch in changes.items():
        name = Path(path).name
        if is_inert(path):
            continue
        if not path.endswith(".py"):
            an.run_all_reasons.append(f"non-Python file changed: {path}")
            continue
        if name == "conftest.py" or "conftest" in name:
            an.run_all_reasons.append(f"conftest changed: {path}")
            continue
        if not is_test_file(path) and ch["status"] != "D":
            an.production.add(path)
        if ch.get("wholesale"):
            # a test module's import-time code is its own tests' concern
            # (conftest is the sanctioned shared hook and has its own rule),
            # so it is never a reason to run everything
            if is_test_file(path):
                select_test_file(path, f"test file {ch['wholesale']}: {path}")
            else:
                an.wholesale.add(path)
            continue
        if ch["status"] == "A":
            # brand-new file: nothing maps to it; select tests defined in it
            if is_test_file(path):
                select_test_file(path, f"new test file {path}")
            else:
                an.wholesale.add(path)
            continue
        if ch["status"] == "D":
            an.wholesale.add(path)
            continue

        # modified file: map changed lines -> functions, in NEW and each OLD version
        module_level = set()
        unreadable = False
        new_src = now.text(path)
        if new_src is None:
            unreadable = True
        else:
            hit, uncovered = funcs_touching(function_spans(new_src), ch["new"], new_src)
            an.changed_funcs |= {(path, q) for q in hit}
            module_level |= uncovered
        for source, old_lines in ch["old"].items():
            old_src = old_source(source, path)
            if old_src is None:
                unreadable = True
                continue
            hit, uncovered = funcs_touching(function_spans(old_src), old_lines, old_src)
            an.changed_funcs |= {(path, q) for q in hit}
            module_level |= uncovered

        if is_test_file(path):
            # any change to a test module selects that module's tests; see the
            # wholesale branch above for why it never escalates to run-all
            an.changed_funcs -= {(p, q) for p, q in an.changed_funcs if p == path}
            select_test_file(
                path,
                f"module-level change in test file: {path}" if module_level or unreadable
                else f"test file changed: {path}",
            )
        elif module_level or unreadable:
            # import-time code changed: every test importing this file is suspect
            an.wholesale.add(path)
    return an


# --- history: the second signal ----------------------------------------------

def recent_status_changes(con, window: int) -> dict[str, str]:
    """test_id -> reason, for every live test whose outcome flipped (passed
    <-> failed, against its previous outcome, in the order the runs
    happened) in one of the last `window` rollups of own runs. Coverage
    cannot see a test broken by another test's leftovers (poc-results
    finding 2: a selected failure skipped its cleanup and five tests that
    never touch the changed code failed after it); history can, once a run
    has observed the flip, and keeps selecting the test until `window`
    rollups pass without one. Foreign runs (another branch's CI jobs) never
    count: their outcomes describe code this line of history may never
    have."""
    if window <= 0:
        return {}
    seqs = [q for (q,) in con.execute(
        "SELECT DISTINCT rollup_seq FROM runs WHERE lineage='own' "
        "ORDER BY rollup_seq DESC LIMIT ?", (window,)
    )]
    if not seqs:
        return {}
    low = min(seqs)
    rows = con.execute(
        "WITH h AS ("
        " SELECT h.test_id, h.run_id, r.rollup_seq, h.status,"
        "  COALESCE(r.finished_at, 0) AS t,"
        "  LAG(h.status) OVER (PARTITION BY h.test_id"
        "   ORDER BY COALESCE(r.finished_at, 0), h.run_id) AS prev"
        " FROM history h JOIN runs r ON r.id = h.run_id"
        " WHERE r.lineage = 'own' AND h.status IN ('passed', 'failed') AND h.test_id IN ("
        "  SELECT h2.test_id FROM history h2 JOIN runs r2 ON r2.id = h2.run_id"
        "  WHERE r2.lineage = 'own' AND r2.rollup_seq >= ?))"
        " SELECT t.test_id, h.prev, h.status, h.run_id FROM h JOIN tests t ON t.id = h.test_id"
        " WHERE h.rollup_seq >= ? AND h.prev IS NOT NULL AND h.prev != h.status"
        " AND t.retired_run IS NULL ORDER BY h.t, h.run_id",
        (low, low),
    ).fetchall()
    return {  # the latest flip wins
        test_id: f"recent status change: {prev} -> {status} in run #{run_id}"
        for test_id, prev, status, run_id in rows
    }


def _tree(commit: str | None, dirty: str | None) -> str:
    return (commit or "?")[:8] + ("" if dirty in (None, "{}") else " + dirty files")


def _flaky_horizon(con, window: int) -> int | None:
    """The rollup sequence number flaky evidence must be newer than, or None
    when nothing can be flaky (window 0, an empty map)."""
    top = con.execute("SELECT MAX(rollup_seq) FROM runs").fetchone()[0]
    return None if window <= 0 or top is None else top - window


def flaky_tests(con, window: int = config.DEFAULTS["flaky_window"]) -> dict[str, dict]:
    """test_id -> evidence, for every live test that both passed and failed
    on one tree within the last `window` rollups: the same commit, the same
    dirty content, the same recorder (python, pytest), in runs the tree did
    not change under. Same code, both outcomes: the difference is not in the
    code. Any lineage counts (a flake on another branch's tree is still a
    flake); evidence older than the window expires, so a test fixed for good
    stops being flaky."""
    horizon = _flaky_horizon(con, window)
    if horizon is None:
        return {}
    rows = con.execute(
        "SELECT t.test_id, r.commit_sha, r.dirty_files, SUM(h.status = 'passed'), "
        "SUM(h.status = 'failed'), MAX(h.run_id) "
        "FROM history h JOIN runs r ON r.id = h.run_id JOIN tests t ON t.id = h.test_id "
        "WHERE h.status IN ('passed', 'failed') AND r.tree_changed = 0 "
        "AND r.commit_sha IS NOT NULL AND t.retired_run IS NULL AND r.rollup_seq > ? "
        "GROUP BY h.test_id, r.commit_sha, r.dirty_files, r.recorder "
        "HAVING SUM(h.status = 'passed') > 0 AND SUM(h.status = 'failed') > 0",
        (horizon,),
    ).fetchall()
    out: dict[str, dict] = {}
    for test_id, commit, dirty, n_pass, n_fail, last in rows:
        if test_id not in out or last > out[test_id]["last_run"]:
            out[test_id] = {"tree": _tree(commit, dirty), "passed": n_pass, "failed": n_fail,
                            "last_run": last}
    return out


def test_history(con, test_id: str, limit: int = 10) -> list[dict]:
    """A test's latest outcomes, oldest first: the receipt for a flake.
    Outcomes from another line of history are marked foreign."""
    rows = con.execute(
        "SELECT h.run_id, r.commit_sha, r.dirty_files, h.status, r.lineage FROM history h "
        "JOIN runs r ON r.id = h.run_id JOIN tests t ON t.id = h.test_id "
        "WHERE t.test_id = ? ORDER BY COALESCE(r.finished_at, 0) DESC, h.run_id DESC LIMIT ?",
        (test_id, limit),
    ).fetchall()
    return [{"run": run_id, "tree": _tree(sha, dirty), "status": status}
            | ({"foreign": True} if lineage == "foreign" else {})
            for run_id, sha, dirty, status, lineage in reversed(rows)]


def contradicted_on_tree(con, failed: list[str], tree: dict, recorder: str,
                         window: int = config.DEFAULTS["flaky_window"]) -> dict[str, dict]:
    """Failures that contradict a pass recorded on this very tree (a
    provenance snapshot: commit + dirty content) with this recorder, within
    the last `window` rollups: a flake, caught the first time it contradicts
    itself rather than after the next rollup."""
    horizon = _flaky_horizon(con, window)
    if not failed or tree.get("commit") is None or horizon is None:
        return {}
    dirty = json.dumps({p: [h, h] for p, h in tree["dirty"].items()}, sort_keys=True)
    out = {}
    for t in failed:
        passes = con.execute(
            "SELECT COUNT(*) FROM history h JOIN runs r ON r.id = h.run_id "
            "JOIN tests tt ON tt.id = h.test_id WHERE tt.test_id = ? AND h.status = 'passed' "
            "AND r.commit_sha = ? AND r.dirty_files = ? AND r.recorder = ? "
            "AND r.tree_changed = 0 AND r.rollup_seq > ?",
            (t, tree["commit"], dirty, recorder, horizon),
        ).fetchone()[0]
        if passes:
            out[t] = {"tree": _tree(tree["commit"], dirty), "passed": passes, "failed": 1,
                      "history": test_history(con, t)}
    return out


# --- the map-staleness safety net ----------------------------------------------

def map_age(con, repo: Path, head: str | None) -> dict:
    """How far HEAD (or --head) has moved on since the commit the last rollup
    recorded: the receipt's map age."""
    commit = mapdb.meta(con).get("rollup_commit") or None
    age: dict = {"commit": commit[:8] if commit else None, "commits_behind_head": None}
    if commit is None:
        age["unknown"] = "the last rollup recorded no commit"
    elif provenance.rev_parse(repo, commit) is None:
        age["unknown"] = (f"map commit {commit[:8]} is not in this repository"
                          f"{provenance.shallow_hint(repo)}")
    else:
        age["commits_behind_head"] = provenance.count_commits(repo, commit, head or "HEAD")
    return age


class EvidenceFiles:
    """Whether a path existed in every tree the map's evidence was recorded
    on (each contributing run's commit plus the dirty files it saw)."""

    def __init__(self, repo: Path, contributing: list[dict]):
        self.repo = repo
        self.trees = [
            (r["commit_sha"], {p: he for p, (_, he) in r["dirty_files"].items()})
            for r in contributing
        ]
        self._at: dict[tuple[str, str], bool] = {}

    def existed(self, path: str) -> bool:
        if not self.trees:
            return False
        for commit, dirty in self.trees:
            if path in dirty:
                if dirty[path] is None:
                    return False
                continue
            if commit is None:
                return False
            if (commit, path) not in self._at:
                self._at[commit, path] = provenance.exists_at(self.repo, commit, path)
            if not self._at[commit, path]:
                return False
        return True


def unseen_changes(an: "Analysis", seen_funcs: set, seen_files: set, files: EvidenceFiles) -> None:
    """A changed function the map has never seen selects nothing by coverage:
    a silent miss for code added after the map was built (and for a new
    method that overrides an inherited one). Broaden: to every test that
    touches its file, or — when the map has never seen the file either and
    the file did not exist when the evidence was recorded — to everything.
    A file that did exist then and that no recorded run ever executed is
    reachable only through some other change, which the diff shows."""
    for path, qual in sorted(an.changed_funcs):
        if (path, qual) not in seen_funcs and path in seen_files:
            an.unseen.setdefault(path, []).append(qual)
    for path in sorted(an.production - seen_files):
        if not files.existed(path):
            an.run_all_reasons.append(
                f"changed file not in the map: {path} (it did not exist when the map's "
                "evidence was recorded)"
            )


def broadening(path: str, quals: list[str]) -> str:
    more = f" (+{len(quals) - 1} more)" if len(quals) > 1 else ""
    return (f"changed function not in the map: {path}::{quals[0]}{more}; "
            f"every test that touches {path} runs")


# --- selection ---------------------------------------------------------------

def select(db_path: Path, repo: Path, base: str | None = None, head: str | None = None, *,
           history_window: int = config.DEFAULTS["history_window"],
           max_map_age: int = config.DEFAULTS["max_map_age"],
           flaky_window: int = config.DEFAULTS["flaky_window"]) -> dict:
    if provenance.git_head(repo) is None:
        return {"error": f"not a git repository with commits: {repo}", "mode": "error"}
    for rev in (base, head):
        if rev is not None and provenance.rev_parse(repo, rev) is None:
            return {"error": f"unknown revision: {rev}", "mode": "error"}
    con = mapdb.connect(str(db_path))
    ev, contributing = evidence_summary(con, repo)
    now = _Now(repo, head)

    # live tests whose module still exists (deleted modules cannot be run)
    if head is None:
        exists_cache: dict[str, bool] = {}

        def module_exists(mod: str) -> bool:
            if mod not in exists_cache:
                exists_cache[mod] = (repo / mod).exists()
            return exists_cache[mod]
    else:
        tree_paths = set(git(repo, "ls-tree", "-r", "--name-only", head).splitlines())

        def module_exists(mod: str) -> bool:
            return mod in tree_paths

    # test_id -> (mapped, deps_run). A test only ever seen by results-only
    # runs has no dependency evidence at all: it is unmapped, so it always runs
    all_tests: dict[str, tuple[int, int | None]] = {}
    missing_modules: set[str] = set()
    for test_id, mapped, deps_run in con.execute(
        "SELECT test_id, CASE WHEN deps_set IS NULL THEN 0 ELSE mapped END, deps_run "
        "FROM tests WHERE retired_run IS NULL AND last_run IS NOT NULL AND test_id != ?",
        (mapdb.COLLECTION,),
    ):
        mod = test_id.split("::", 1)[0]
        if module_exists(mod):
            all_tests[test_id] = (mapped, deps_run)
        else:
            missing_modules.add(mod)
    ev["missing_modules"] = sorted(missing_modules)
    coll_funcs, coll_files = collection_deps(con)
    recent = {
        t: r for t, r in recent_status_changes(con, history_window).items() if t in all_tests
    }
    flaky = {t: f | {"history": test_history(con, t)}
             for t, f in flaky_tests(con, flaky_window).items() if t in all_tests}
    history = {"window": history_window, "recent_changes": len(recent), "flaky": len(flaky),
               "flaky_window": flaky_window}
    seen_funcs = set(con.execute(
        "SELECT DISTINCT f.path, fn.qualname FROM funcs fn JOIN files f ON f.id = fn.file_id"
    ))
    seen_files = {p for p, _ in seen_funcs}
    evidence_files = EvidenceFiles(repo, contributing)
    ev["unmapped_tests"] = sum(1 for mapped, _ in all_tests.values() if not mapped)
    ev["map_age"] = age = map_age(con, repo, head) | {"max_map_age": max_map_age}
    stale: list[str] = []
    if age["commits_behind_head"] is None:
        stale.append(f"map age unknown: {age['unknown']}")
    elif age["commits_behind_head"] > max_map_age:
        stale.append(f"map is {age['commits_behind_head']} commits behind HEAD "
                     f"(max_map_age {max_map_age})")

    missing: dict[int, str] = {}  # evidence run -> its commit, absent from this repository
    trees = build_trees(repo, contributing, base, ev, missing)
    untracked = provenance.dirty_files(repo) if head is None else {}
    git_cache: dict[str, dict] = {}
    analyses: list[Analysis] = []
    for tree in trees:
        an = Analysis()
        changes = tree_changes(repo, tree, now, con, git_cache, untracked, an)
        analyze(repo, con, now, changes, all_tests, an)
        # import/collection-time execution: a changed function that runs during
        # collection (decorators, module-level declarations) can break any test
        # before it even starts — no per-test attribution possible, so run all.
        for path, qual in sorted(an.changed_funcs):
            if (path, qual) in coll_funcs:
                an.run_all_reasons.append(f"executed at import/collection time: {path}::{qual}")
        for path in sorted(an.wholesale):
            if path in coll_files:
                an.run_all_reasons.append(f"module-level change in import-time file: {path}")
        unseen_changes(an, seen_funcs, seen_files, evidence_files)
        analyses.append(an)

    ev["trees"] = [t.summary() for t in trees]
    ev["dirty_rule"] = {
        "unchanged_since_observation": sorted({p for a in analyses for p in a.unchanged}),
        "forced_wholesale": sorted({p for a in analyses for p in a.forced}),
    }
    run_all_reasons = sorted({r for a in analyses for r in a.run_all_reasons} | set(stale))
    unseen: dict[str, list[str]] = {}
    for an in analyses:
        for path, quals in an.unseen.items():
            unseen[path] = sorted(set(unseen.get(path, [])) | set(quals))
    broadened = [broadening(path, quals) for path, quals in sorted(unseen.items())]
    changed_funcs = sorted({f"{p}::{q}" for a in analyses for p, q in a.changed_funcs})
    wholesale = sorted({p for a in analyses for p in a.wholesale})
    # known tests the selector expects not to exist any more: they are
    # selected (their module runs whole), and cannot produce a result
    vanished = sorted({t for a in analyses for t in a.vanished if t in all_tests})

    if run_all_reasons:
        # a changed test module is still targeted whole, so its new tests run
        # and its deleted ones never reach pytest as node ids
        run_all_files: dict[str, str] = {}
        for an in analyses:
            run_all_files.update(an.selected_files)
        con.close()
        return {
            "mode": "run_all",
            "reasons": run_all_reasons,
            "selected": {t: "run_all" for t in sorted(all_tests)},
            "selected_files": run_all_files,
            "targets": sorted(run_all_files) + sorted(
                t for t in all_tests if t.split("::", 1)[0] not in run_all_files
            ),
            "n_selected": len(all_tests),
            "n_skipped": 0,
            "n_total": len(all_tests),
            "changed_functions": changed_funcs,
            "changed_files_wholesale": wholesale,
            "vanished": vanished,
            "broadened": broadened,
            "flaky": flaky,
            "history": history,
            "evidence": ev,
        }

    known_runs = {rid for t in trees if t.run_ids for rid in t.run_ids} | set(missing)
    selected: dict[str, str] = {}  # test_id -> reason
    selected_files: dict[str, str] = {}
    for t, (_, deps_run) in all_tests.items():
        if deps_run in missing:
            selected[t] = (f"evidence commit {missing[deps_run][:8]} is not in this repository: "
                           "nothing to diff it against")
    for tree, an in zip(trees, analyses):
        if tree.run_ids is None:
            in_tree = set(all_tests)
        else:
            in_tree = {
                t for t, (_, deps_run) in all_tests.items()
                if deps_run in tree.run_ids or deps_run not in known_runs
            }
        for path, qual in an.changed_funcs:
            rows = con.execute(
                "SELECT t.test_id FROM tests t JOIN current_links l ON l.test_id=t.id "
                "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
                "WHERE f.path=? AND fn.qualname=?",
                (path, qual),
            )
            for (t,) in rows:
                if t in in_tree:
                    selected.setdefault(t, f"touches changed function {path}::{qual}")
        for path in an.wholesale:
            rows = con.execute(
                "SELECT DISTINCT t.test_id FROM tests t JOIN current_links l ON l.test_id=t.id "
                "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
                "WHERE f.path=?",
                (path,),
            )
            for (t,) in rows:
                if t in in_tree:
                    selected.setdefault(t, f"imports/touches changed file {path}")
        for path, quals in an.unseen.items():
            reason = broadening(path, quals)
            rows = con.execute(
                "SELECT DISTINCT t.test_id FROM tests t JOIN current_links l ON l.test_id=t.id "
                "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
                "WHERE f.path=?",
                (path,),
            )
            for (t,) in rows:
                if t in in_tree:
                    selected.setdefault(t, reason)
        for path, reason in an.test_files.items():
            for t in in_tree:
                if t.startswith(path + "::"):
                    selected.setdefault(t, reason)
        selected_files.update(an.selected_files)

    # history, the second signal: a test whose outcome flipped recently runs
    # again, whatever its coverage says (cross-test pollution, flakes)
    for t, reason in recent.items():
        selected.setdefault(t, reason)

    # conservative: unmapped tests always run
    for t, (mapped, _) in all_tests.items():
        if not mapped:
            selected.setdefault(t, "unmapped test (conservative)")

    skipped = {
        t: "no overlap between this test's coverage map and the diff"
        for t in all_tests
        if t not in selected
    }
    # a whole-file target subsumes that file's individual node ids
    targets = sorted(selected_files) + sorted(
        t for t in selected if t.split("::", 1)[0] not in selected_files
    )
    con.close()
    return {
        "mode": "select",
        "changed_functions": changed_funcs,
        "changed_files_wholesale": wholesale,
        "selected": {t: selected[t] for t in sorted(selected)},
        "selected_files": selected_files,
        "targets": targets,
        "n_selected": len(selected),
        "n_skipped": len(skipped),
        "n_total": len(all_tests),
        "vanished": vanished,
        "broadened": broadened,
        "flaky": flaky,
        "history": history,
        "evidence": ev,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--base", default=None, help="diff base (default: the trees the evidence saw)")
    ap.add_argument("--head", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    db = args.db or journal.map_path(args.repo)  # read-only: `fastest rollup` first
    result = select(db, args.repo.resolve(), args.base, args.head)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        ev = result["evidence"]
        print(
            f"evidence: {ev['runs']} runs, commits {[c[:8] for c in ev['evidence_commits']]}, "
            f"{ev['commits_behind']} behind HEAD, {len(ev['trees'])} tree(s) ({ev['base_source']})"
        )
        for w in ev["warnings"]:
            print(f"  ! {w}")
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
            for p, why in result["selected_files"].items():
                print(f"  RUN  {p}  [{why}]")


if __name__ == "__main__":
    main()
