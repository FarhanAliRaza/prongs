"""The CI artifact flow: carry the map and journal files between CI jobs.

  python -m fastest ci save DIR [--no-map] [--no-journals]
  python -m fastest ci restore PATH [PATH ...] [--no-fetch] [--remote NAME]

A CI job starts from a fresh clone, so everything fastest knows has to
travel as an artifact (a cache entry, an uploaded directory). An artifact is
a directory:

  manifest.json       what is in it: the map's newest own run and commit, the
                      journal files, the job's HEAD, versions
  map.sqlite          a consistent, compact snapshot of the map (VACUUM INTO)
  journal/*.sqlite    the journal files this job wrote (rolled up or not)

The flow (examples/github-actions.yml has it as a workflow):

  main job   restore the last map artifact (and recent PR journal
             artifacts), run the suite with the recorder, `fastest rollup`,
             `ci save` the map and this job's journals
  PR job     restore the last map artifact, `fastest run` (its exit code is
             the gate), `ci save --no-map` this job's journals, for the next
             main job to fold

`restore` makes a fresh clone ready to select:

  * it installs the newest map among the artifacts when there is no local
    map, or the local one holds no run the artifact lacks (`newest` means
    the newest own run the map holds)
  * it makes sure the clone has the commits that map's evidence was observed
    on: in a shallow clone it deepens the history (`git fetch --deepen`,
    growing) until they are present, and unshallows as a last resort; a
    commit still missing is left to the selector, which selects every test
    whose evidence it is, and runs everything if it is the map's own commit
  * it copies each journal file into the journal by lineage: a run whose
    commit is an ancestor of HEAD is this line of history's own evidence; any
    other (another branch's CI job, a pull request's test merge commit) is
    foreign and folds as history only — outcomes for flaky detection,
    never evidence (see mapdb)
  * it skips runs the map already holds, since rollup folds by journal key
    and a journal artifact is often downloaded more than once
  * it records the keys it imported (.fastest/ci/restored.json), so `save`
    carries only the journal files this job wrote

Everything the selector needs is in the map and git; journal files are read
with journal.read_run(), which needs no map at all.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from fastest import __version__, journal, mapdb, provenance

FORMAT = 1
MANIFEST = "manifest.json"
MAP = "map.sqlite"
JOURNAL = "journal"


def _restored_path(repo: Path) -> Path:
    return journal.state_dir(repo) / "ci" / "restored.json"


def _restored(repo: Path) -> set[str]:
    try:
        return set(json.loads(_restored_path(repo).read_text()))
    except (OSError, ValueError):
        return set()


def _newest_run(con: sqlite3.Connection) -> dict | None:
    row = con.execute(
        "SELECT journal_key, finished_at, commit_sha FROM runs WHERE lineage='own' "
        "ORDER BY COALESCE(finished_at, 0) DESC, id DESC LIMIT 1"
    ).fetchone()
    return {"key": row[0], "finished_at": row[1], "commit": row[2]} if row else None


def _map_summary(path: Path) -> dict:
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        m = mapdb.meta(con)
        return {
            "schema": int(m.get("schema_version") or 0),
            "rollup_commit": m.get("rollup_commit") or None,
            "newest_run": _newest_run(con),
            "runs": con.execute("SELECT COUNT(*) FROM runs WHERE lineage='own'").fetchone()[0],
            "tests": con.execute(
                "SELECT COUNT(*) FROM tests WHERE retired_run IS NULL AND last_run IS NOT NULL "
                "AND test_id != ?", (mapdb.COLLECTION,)).fetchone()[0],
        }
    finally:
        con.close()


def _keys(path: Path) -> set[str]:
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {k for (k,) in con.execute("SELECT journal_key FROM runs") if k}
    finally:
        con.close()


# --- save ----------------------------------------------------------------------

def save(repo: Path, out: Path, *, with_map: bool = True, with_journals: bool = True) -> dict:
    """Write an artifact directory: a snapshot of the map (pending journal
    files rolled up first) and the journal files this checkout wrote — own
    files, pending or consumed, that no `restore` imported."""
    repo, out = Path(repo), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    jdir = journal.journal_dir(repo)
    manifest: dict = {
        "format": FORMAT,
        "fastest": __version__,
        "created_at": time.time(),
        "head": provenance.git_head(repo),
        "map": None,
        "journals": [],
    }
    if with_map:
        db = journal.map_path(repo)
        mapdb.rollup(db, jdir)  # the snapshot holds this job's runs
        if db.exists():
            target = out / MAP
            target.unlink(missing_ok=True)
            con = mapdb.connect(str(db))
            try:
                con.execute("VACUUM INTO ?", (str(target),))
            finally:
                con.close()
            manifest["map"] = _map_summary(target) | {"bytes": target.stat().st_size}
    if with_journals:
        imported = _restored(repo)
        dest = out / JOURNAL
        for f in journal.pending(jdir) + journal.pending(jdir / "consumed"):
            try:
                run = journal.read_run(f)
            except (sqlite3.DatabaseError, FileNotFoundError):
                continue
            if run["key"] in imported:
                continue
            dest.mkdir(exist_ok=True)
            shutil.copy2(f, dest / f.name)
            manifest["journals"].append({
                "key": run["key"], "commit": run["commit_sha"], "scope": run["scope"],
                "mode": run["mode"], "finished_at": run["finished_at"],
                "n_observed": run["n_observed"],
            })
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return {"saved": str(out), "map": manifest["map"],
            "journals": [j["key"] for j in manifest["journals"]]}


# --- restore -------------------------------------------------------------------

def find_artifacts(paths) -> list[Path]:
    """Artifact directories at or under each path (a download step often
    nests every artifact in a directory of its own)."""
    found: list[Path] = []
    for p in map(Path, paths):
        if (p / MANIFEST).is_file():
            found.append(p)
        elif p.is_dir():
            found += sorted(m.parent for m in p.rglob(MANIFEST))
    return list(dict.fromkeys(found))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def ensure_history(repo: Path, commits, *, remote: str = "origin", fetch: bool = True,
                   max_rounds: int = 6) -> dict:
    """Make the commits present, fetching as little history as it takes: in
    a shallow clone deepen (64, 256, ... commits) until they are there, and
    unshallow as a last resort; a commit on no fetched ref is asked for by
    name. A pull request's test merge commit (what CI checks out) is on no
    branch, so while HEAD's own parents are missing its history is deepened
    by name too: counting the map's age and telling lineage walk from HEAD.
    Returns what was missing, what was fetched, what is still missing."""
    need = sorted({c for c in commits if c})
    missing = [c for c in need if provenance.rev_parse(repo, c) is None]
    head = provenance.git_head(repo)

    def head_cut() -> bool:  # a shallow clone whose HEAD came without its parents
        return (head is not None and provenance.is_shallow(repo)
                and _git(repo, "rev-parse", "-q", "--verify", f"{head}^").returncode != 0)

    out: dict = {"shallow": provenance.is_shallow(repo), "needed": len(need),
                 "missing": len(missing), "fetches": []}
    if not (missing or need and head_cut()) or not fetch:
        out["still_missing"] = missing
        return out

    def fetch_(*args: str) -> None:
        p = _git(repo, "fetch", "--no-tags", "--quiet", *args)
        out["fetches"].append({"args": list(args), "ok": p.returncode == 0}
                              | ({"error": p.stderr.strip()[-300:]} if p.returncode else {}))

    depth, rounds = 64, 0
    while (missing or head_cut()) and provenance.is_shallow(repo) and rounds < max_rounds:
        fetch_(f"--deepen={depth}", remote)
        if head_cut():
            fetch_(f"--deepen={depth}", remote, head)
        missing = [c for c in missing if provenance.rev_parse(repo, c) is None]
        depth, rounds = depth * 4, rounds + 1
    if missing and provenance.is_shallow(repo):
        fetch_("--unshallow", remote)
        missing = [c for c in missing if provenance.rev_parse(repo, c) is None]
    for c in list(missing):  # a commit on another branch: ask for it by name
        fetch_(remote, c)
    missing = [c for c in missing if provenance.rev_parse(repo, c) is None]
    out["still_missing"] = missing
    out["shallow_after"] = provenance.is_shallow(repo)
    return out


def _evidence_commits(db: Path) -> set[str]:
    """Every commit the selector will diff from: the map's own commit and
    each run a live test's evidence (or the last full run) came from."""
    con = mapdb.connect(str(db))
    try:
        commits = {r["commit_sha"] for r in mapdb.contributing_runs(con)}
        commits.add(mapdb.meta(con).get("rollup_commit"))
    finally:
        con.close()
    return {c for c in commits if c}


def _is_own(repo: Path, commit: str | None, head: str | None) -> bool:
    """A run is this line of history's own when its commit is an ancestor of
    HEAD (or HEAD itself); anything else was observed on another branch."""
    if not commit or not head or provenance.rev_parse(repo, commit) is None:
        return False
    return _git(repo, "merge-base", "--is-ancestor", commit, head).returncode == 0


def restore(repo: Path, paths, *, fetch: bool = True, remote: str = "origin") -> dict:
    """Install artifacts into this checkout's state directory; see the
    module docstring for the rules."""
    repo = Path(repo)
    t0 = time.monotonic()
    artifacts = find_artifacts(paths)
    out: dict = {"artifacts": [str(a) for a in artifacts], "map": None,
                 "journals": {"own": [], "foreign": [], "known": 0, "unreadable": []},
                 "warnings": []}
    db, jdir = journal.map_path(repo), journal.journal_dir(repo)

    # 1. the map: the newest one offered, unless the local map knows more
    offers = []
    for a in artifacts:
        if not (a / MAP).is_file():
            continue
        try:
            summary = _map_summary(a / MAP)
        except sqlite3.DatabaseError as e:
            out["warnings"].append(f"{a / MAP}: unreadable map ({e})")
            continue
        if summary["schema"] > mapdb.SCHEMA_VERSION:
            out["warnings"].append(
                f"{a / MAP}: schema {summary['schema']} is newer than this fastest "
                f"({mapdb.SCHEMA_VERSION}); skipped")
            continue
        newest = summary["newest_run"] or {}
        offers.append(((newest.get("finished_at") or 0.0, str(a)), a, summary))
    if offers:
        _, a, summary = max(offers, key=lambda o: o[0])
        if db.exists() and not _keys(db) <= _keys(a / MAP):
            out["map"] = {"installed": False, "from": str(a),
                          "reason": "the local map holds runs the artifact lacks: kept it"}
        else:
            db.parent.mkdir(parents=True, exist_ok=True)
            tmp = db.with_name(f".tmp-{os.getpid()}-{db.name}")
            shutil.copyfile(a / MAP, tmp)
            for suffix in ("-wal", "-shm"):  # a stale WAL would be replayed onto the new map
                Path(f"{db}{suffix}").unlink(missing_ok=True)
            os.replace(tmp, db)
            con = mapdb.connect(str(db))  # migrates an older schema
            con.execute("PRAGMA journal_mode=WAL")
            con.close()
            out["map"] = {"installed": True, "from": str(a)} | summary

    # 2. journal files, by lineage
    known = _keys(db) if db.exists() else set()
    present = {p.stem for p in journal.pending_all(jdir) + journal.pending(jdir / "consumed")}
    candidates = []
    for a in artifacts:
        for f in sorted((a / JOURNAL).glob("*.sqlite")) if (a / JOURNAL).is_dir() else []:
            try:
                run = journal.read_run(f)
            except (sqlite3.DatabaseError, FileNotFoundError, ValueError):
                out["journals"]["unreadable"].append(str(f))
                continue
            if run["key"] in known or run["key"] in present:
                out["journals"]["known"] += 1
                continue
            present.add(run["key"])
            candidates.append((f, run))

    # 3. history: the map's evidence commits, and the journals' (to tell
    # lineage: a commit that stays missing belongs to another branch)
    evidence = _evidence_commits(db) if db.exists() else set()
    out["history"] = ensure_history(
        repo, evidence | {run["commit_sha"] for _, run in candidates}, remote=remote, fetch=fetch)
    lost = sorted(evidence & set(out["history"]["still_missing"]))
    if lost:
        out["warnings"].append(
            f"{len(lost)} commit(s) the map's evidence was observed on are not in this "
            f"clone ({', '.join(c[:8] for c in lost)}): the tests observed there will run")

    head = provenance.git_head(repo)
    imported = _restored(repo)
    for f, run in candidates:
        lineage = "own" if _is_own(repo, run["commit_sha"], head) else "foreign"
        dest = jdir if lineage == "own" else jdir / journal.FOREIGN
        dest.mkdir(parents=True, exist_ok=True)
        tmp = dest / f".tmp-{f.name}"
        shutil.copyfile(f, tmp)
        os.replace(tmp, dest / f"{run['key']}.sqlite")
        out["journals"][lineage].append(run["key"])
        imported.add(run["key"])
    if candidates:
        rp = _restored_path(repo)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(sorted(imported)))
    out["wall_s"] = round(time.monotonic() - t0, 3)
    return out
