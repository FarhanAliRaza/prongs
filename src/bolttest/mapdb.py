"""SQLite storage for the per-test dependency map: the roll-up of the journal.

Runs are recorded as journal files (journal.py): append-only, one file per
run, any number of writers. map.sqlite is derived from them by rollup(), the
only writer of the map, and is read by the selector:

  runs          one row per rolled-up run: its journal key, provenance (commit,
                the files that differed from HEAD with content hashes at start
                and end of the run, recorder fingerprint), scope, mode,
                worktree, counts, the rollup that folded it, and its lineage:
                'own', or 'foreign' for a run imported from another line of
                history (a CI job on another branch), which adds history rows
                and nothing else
  history       one row per (run, test): outcome, duration, and the dependency
                set when the run recorded coverage (NULL when it did not) —
                the per-test result history
  dep_sets      content-addressed sets of project functions, shared between
  dep_members   history rows, so an unchanged dependency set costs one row
  blobs         content of files that were dirty at observation
  tests         current view per test, from own runs only: the newest
                outcome and duration, the newest dependency set and the run it
                came from (`deps_run`: the tree that evidence was observed
                on), first/last run, counters, retirement
  current_links view over tests x dep_members: the inverted map
  meta          schema version; the last rollup's sequence number; the
                newest own run in the map and its commit

Interned: files, funcs ((file, qualname, lineno) -> int).

rollup() folds every pending journal file in one write transaction, then
moves the files to journal/consumed/. Runs are keyed by their journal key, so
a rollup interrupted between its commit and the move never folds a run twice;
concurrent rollups serialize on the write lock and readers are never blocked
(WAL). fold_run() is the one fold, used by rollup() and, from history alone,
by rebuild_rollup(). The fold is order-independent: the newest observation
(by finish time) wins, so a journal file that arrives late — a CI artifact
from a job that finished before the last one rolled up — adds its history
without rolling the current view back to an older tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import time
from pathlib import Path

from bolttest import journal

SCHEMA_VERSION = 4
COLLECTION = journal.COLLECTION  # pseudo-test: code executed at import/collection time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS funcs (
    id INTEGER PRIMARY KEY, file_id INTEGER, qualname TEXT, lineno INTEGER,
    UNIQUE(file_id, qualname, lineno)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    journal_key TEXT,        -- the journal file it was folded from
    started_at REAL, finished_at REAL,
    scope TEXT,              -- 'full' | 'partial' | 'collect'
    mode TEXT,               -- 'coverage' | 'results'
    commit_sha TEXT,         -- HEAD when the run started; NULL outside git
    worktree TEXT,           -- the checkout that ran it
    dirty_files TEXT,        -- JSON {path: [hash_at_start, hash_at_end]}
    tree_changed INTEGER,    -- HEAD moved or the dirty set changed mid-run
    recorder TEXT,           -- recorder fingerprint (bolttest/python/pytest)
    args TEXT,               -- JSON: pytest invocation args
    n_collected INTEGER, n_observed INTEGER,
    collect_errors TEXT,     -- JSON {node id: last line of the error}
    rollup_seq INTEGER,      -- the rollup that folded it
    lineage TEXT NOT NULL DEFAULT 'own'  -- 'own' | 'foreign' (history only)
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_by_key ON runs(journal_key);
CREATE TABLE IF NOT EXISTS blobs (   -- content of files that were dirty at observation
    hash TEXT PRIMARY KEY, content BLOB
);
CREATE TABLE IF NOT EXISTS dep_sets (
    id INTEGER PRIMARY KEY, hash BLOB UNIQUE, size INTEGER
);
CREATE TABLE IF NOT EXISTS dep_members (
    set_id INTEGER, func_id INTEGER,
    PRIMARY KEY (set_id, func_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS dep_members_by_func ON dep_members(func_id);
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY, test_id TEXT UNIQUE,
    status TEXT, duration REAL, mapped INTEGER, deps_set INTEGER, deps_run INTEGER,
    first_run INTEGER, last_run INTEGER, retired_run INTEGER,
    n_obs INTEGER NOT NULL DEFAULT 0, n_failed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tests_by_set ON tests(deps_set);
CREATE TABLE IF NOT EXISTS history (
    run_id INTEGER, test_id INTEGER, status TEXT, duration REAL,
    deps_set INTEGER,        -- NULL: the run recorded no coverage
    PRIMARY KEY (run_id, test_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_by_test ON history(test_id);
CREATE VIEW IF NOT EXISTS current_links AS
    SELECT t.id AS test_id, m.func_id
    FROM tests t JOIN dep_members m ON m.set_id = t.deps_set
    WHERE t.retired_run IS NULL;
"""

RUN_COLUMNS = (
    "id", "journal_key", "started_at", "finished_at", "scope", "mode", "commit_sha",
    "worktree", "dirty_files", "tree_changed", "recorder", "args", "n_collected",
    "n_observed", "collect_errors", "rollup_seq", "lineage",
)


_OBJECTS = {
    "meta", "files", "funcs", "runs", "blobs", "dep_sets", "dep_members", "tests",
    "history", "current_links",
}


def _is_current(con: sqlite3.Connection) -> bool:
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    if not _OBJECTS <= names:
        return False
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    return bool(row) and row[0] == str(SCHEMA_VERSION)


def connect(path: str) -> sqlite3.Connection:
    """Open and return a connection, creating or migrating the schema only
    when it is missing or old: a read (select) never takes the write lock."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    if _is_v1(con):
        _migrate_v1(con)
    elif _is_v2(con):
        _migrate_v2(con)
    if _is_v3(con):
        _migrate_v3(con)
    if not _is_current(con):
        con.execute("PRAGMA journal_mode=WAL")  # persistent: set once per file
        con.executescript(SCHEMA)
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        con.commit()
    return con


def meta(con: sqlite3.Connection) -> dict[str, str]:
    return dict(con.execute("SELECT key, value FROM meta"))


# --- interning ---------------------------------------------------------------

class _Interner:
    """(path relative to the worktree, qualname, lineno) -> funcs.id"""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self._files: dict[str, int] = {}
        self._funcs: dict[tuple, int] = {}

    def file_id(self, path: str) -> int:
        fid = self._files.get(path)
        if fid is None:
            fid = self._files[path] = self.con.execute(
                "INSERT INTO files(path) VALUES(?) "
                "ON CONFLICT DO UPDATE SET path=path RETURNING id",
                (path,),
            ).fetchone()[0]
        return fid

    def func_id(self, path: str, qualname: str, lineno: int) -> int:
        key = (path, qualname, lineno)
        fid = self._funcs.get(key)
        if fid is None:
            fid = self._funcs[key] = self.con.execute(
                "INSERT INTO funcs(file_id, qualname, lineno) VALUES(?,?,?) "
                "ON CONFLICT DO UPDATE SET qualname=qualname RETURNING id",
                (self.file_id(path), qualname, lineno),
            ).fetchone()[0]
        return fid


def intern_test(con: sqlite3.Connection, test_id: str) -> int:
    return con.execute(
        "INSERT INTO tests(test_id) VALUES(?) "
        "ON CONFLICT(test_id) DO UPDATE SET test_id=test_id RETURNING id",
        (test_id,),
    ).fetchone()[0]


def intern_set(con: sqlite3.Connection, func_ids) -> tuple[int, bool]:
    """Content-addressed dependency set -> (set_id, created)."""
    ids = sorted(set(func_ids))
    h = hashlib.blake2b(struct.pack(f"<{len(ids)}q", *ids), digest_size=16).digest()
    row = con.execute("SELECT id FROM dep_sets WHERE hash=?", (h,)).fetchone()
    if row:
        return row[0], False
    set_id = con.execute(
        "INSERT INTO dep_sets(hash, size) VALUES(?,?) RETURNING id", (h, len(ids))
    ).fetchone()[0]
    con.executemany(
        "INSERT INTO dep_members(set_id, func_id) VALUES(?,?)",
        [(set_id, f) for f in ids],
    )
    return set_id, True


def set_members(con: sqlite3.Connection, set_id: int) -> list[int]:
    return [f for (f,) in con.execute(
        "SELECT func_id FROM dep_members WHERE set_id=?", (set_id,)
    )]


# --- rollup ------------------------------------------------------------------

def _order(finished_at, run_id) -> tuple[float, int]:
    """When a run happened, as a sort key: finish time, then fold order."""
    return (finished_at or 0.0, run_id)


def rollup(path, jdir) -> dict:
    """Fold every pending journal file into the map in one write transaction,
    in the order the runs finished; then record the newest own run and its
    commit in meta and move the files to consumed/. Files under
    journal/foreign/ (imported from another line of history, see ci.py) are
    folded as foreign runs. With nothing pending the map is not even opened.

    Returns {'rolled_up', 'rollup_seq', 'runs': [{'run_id', 'key', 'mode',
    'scope', 'lineage', 'n_tests', 'n_new_sets'}], 'already_folded',
    'corrupt', 'commit', 'n_funcs', 'wall_s'}."""
    t0 = time.monotonic()
    out: dict = {"rolled_up": 0, "rollup_seq": None, "runs": [], "already_folded": 0,
                 "corrupt": [], "commit": None}
    sources = (("own", Path(jdir)), ("foreign", Path(jdir) / journal.FOREIGN))
    if not any(journal.pending(d) for _, d in sources):
        return out | {"wall_s": round(time.monotonic() - t0, 3)}
    con = connect(str(path))
    folded, done, bad = [], [], []
    try:
        con.execute("BEGIN IMMEDIATE")  # one rollup at a time
        entries = []
        for lineage, d in sources:
            for f in journal.pending(d):  # listed again under the lock
                try:
                    run = journal.read_run(f)
                except (sqlite3.DatabaseError, ValueError, FileNotFoundError):
                    bad.append(f)
                    continue
                if run["format"] != journal.FORMAT:
                    bad.append(f)
                    continue
                entries.append((run["finished_at"] or 0.0, run["key"], f, run, lineage))
        entries.sort(key=lambda e: e[:2])
        seq = int(meta(con).get("rollup_seq") or 0) + 1
        for _, key, f, run, lineage in entries:
            if con.execute("SELECT 1 FROM runs WHERE journal_key=?", (key,)).fetchone():
                done.append(f)  # folded by a rollup that died before moving it
                continue
            src = journal.open_ro(f)
            try:
                folded.append((f, _fold_file(con, src, run, seq, lineage)))
            finally:
                src.close()
        if folded:
            items = [("rollup_seq", str(seq)), ("rolled_up_at", repr(time.time()))]
            newest = con.execute(
                "SELECT id, commit_sha, worktree FROM runs WHERE lineage='own' "
                "ORDER BY COALESCE(finished_at, 0) DESC, id DESC LIMIT 1"
            ).fetchone()
            if newest:  # the map's age is counted from its newest own evidence
                items += [("rollup_run", str(newest[0])), ("rollup_commit", newest[1] or ""),
                          ("rootpath", (newest[2] or "") + os.sep)]
                out["commit"] = newest[1]
            con.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", items)
            out["rollup_seq"] = seq
        out["n_funcs"] = con.execute("SELECT COUNT(*) FROM funcs").fetchone()[0]
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    journal.consume(jdir, [f for f, _ in folded] + done)
    journal.quarantine(jdir, bad)
    out.update(
        rolled_up=len(folded), runs=[stats for _, stats in folded],
        already_folded=len(done), corrupt=[Path(f).name for f in bad],
        wall_s=round(time.monotonic() - t0, 3),
    )
    return out


def _fold_file(con: sqlite3.Connection, src: sqlite3.Connection, run: dict, seq: int,
               lineage: str = "own") -> dict:
    """Append one journal file's run and history rows, then fold it. A
    foreign run keeps only its outcomes: its dependency sets and dirty-file
    content describe another line of history and are never evidence here."""
    foreign = lineage == "foreign"
    run_id = con.execute(
        "INSERT INTO runs(journal_key, started_at, finished_at, scope, mode, commit_sha, "
        "worktree, dirty_files, tree_changed, recorder, args, n_collected, n_observed, "
        "collect_errors, rollup_seq, lineage) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "RETURNING id",
        (
            run["key"], run["started_at"], run["finished_at"], run["scope"], run["mode"],
            run["commit_sha"], run["worktree"], json.dumps(run["dirty_files"], sort_keys=True),
            int(run["tree_changed"]), run["recorder"], json.dumps(run["args"]),
            run["n_collected"], run["n_observed"],
            json.dumps(run["collect_errors"], sort_keys=True), seq, lineage,
        ),
    ).fetchone()[0]
    deps: dict[int, list[int]] = {}
    if not foreign:
        con.executemany(
            "INSERT OR IGNORE INTO blobs(hash, content) VALUES (?,?)",
            src.execute("SELECT hash, content FROM blobs"),
        )
        interner = _Interner(con)
        func_ids = {
            jid: interner.func_id(p, q, ln)
            for jid, p, q, ln in src.execute("SELECT id, path, qualname, lineno FROM funcs")
        }
        for t, f in src.execute("SELECT test, func FROM links"):
            deps.setdefault(t, []).append(func_ids[f])
    rows, n_tests, n_new = [], 0, 0
    for jid, test_id, status, duration, mapped in src.execute(
        "SELECT id, test_id, status, duration, mapped FROM tests"
    ):
        if foreign and test_id == COLLECTION:
            continue
        set_id = None
        if mapped is not None and not foreign:  # coverage recorded: the test's dependency set
            set_id, created = intern_set(con, deps.get(jid, ()))
            n_new += created
        rows.append((run_id, intern_test(con, test_id), status, duration, set_id))
        n_tests += test_id != COLLECTION
    con.executemany(
        "INSERT INTO history(run_id, test_id, status, duration, deps_set) VALUES (?,?,?,?,?)",
        rows,
    )
    fold_run(con, run_id)
    return {"run_id": run_id, "key": run["key"], "mode": run["mode"], "scope": run["scope"],
            "lineage": lineage, "n_tests": n_tests, "n_new_sets": n_new}


def fold_run(con: sqlite3.Connection, run_id: int) -> None:
    """Fold one run's history rows into the current view. Order-independent:
    every rule compares when runs happened (finish time, then fold order), so
    rollup() can fold a late journal file and rebuild_rollup() can replay
    history in id order, and both reach the same view.

    Rules:
      * a foreign run folds nothing: its history rows are all it adds;
      * an observed test's counters always count the observation; its
        outcome and duration are replaced only by a newer observation, and
        its dependency set, and the run that set came from (`deps_run`), only
        by a newer observation that recorded coverage — a results-only
        observation never moves a test's evidence to a tree whose
        dependencies nobody observed;
      * a 'full' run retires tests it did not observe although their module
        was observed, unless a newer run observed them; tests in modules that
        did not collect at all keep their last evidence, which is what
        surfaces the breakage; a newer observation un-retires a test;
      * the collection pseudo-test is replaced by the newest full/collect run
        and unioned by a partial run, since a partial run only sees the
        import-time code of the modules it collected.
    """
    scope, finished, lineage = con.execute(
        "SELECT scope, finished_at, lineage FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if lineage == "foreign":
        return
    me = _order(finished, run_id)

    def newer(other: int | None, other_finished) -> bool:
        return other is None or me > _order(other_finished, other)

    coll_tid = intern_test(con, COLLECTION)
    rows = con.execute(
        "SELECT h.test_id, h.status, h.duration, h.deps_set, s.size, t.deps_set, "
        "t.last_run, lr.finished_at, t.deps_run, dr.finished_at, t.retired_run, rr.finished_at "
        "FROM history h JOIN tests t ON t.id = h.test_id "
        "LEFT JOIN dep_sets s ON s.id = h.deps_set "
        "LEFT JOIN runs lr ON lr.id = t.last_run "
        "LEFT JOIN runs dr ON dr.id = t.deps_run "
        "LEFT JOIN runs rr ON rr.id = t.retired_run "
        "WHERE h.run_id = ?",
        (run_id,),
    ).fetchall()
    counts, outcomes, evidence, unretire = [], [], [], []
    coll = None
    for (tid, status, duration, set_id, size, cur_set, last, last_t, deps, deps_t,
         retired, retired_t) in rows:
        if tid == coll_tid:
            coll = (set_id, cur_set, deps, deps_t)
            continue
        counts.append((int(status == "failed"), run_id, tid))
        if newer(last, last_t):
            outcomes.append((status, duration, run_id, tid))
        if set_id is not None and newer(deps, deps_t):
            evidence.append((int(size > 0), set_id, run_id, tid))
        if retired is not None and newer(retired, retired_t):
            unretire.append((tid,))
    con.executemany(
        "UPDATE tests SET n_obs=n_obs+1, n_failed=n_failed+?, first_run=COALESCE(first_run, ?) "
        "WHERE id=?", counts,
    )
    con.executemany("UPDATE tests SET status=?, duration=?, last_run=? WHERE id=?", outcomes)
    con.executemany("UPDATE tests SET mapped=?, deps_set=?, deps_run=? WHERE id=?", evidence)
    con.executemany("UPDATE tests SET retired_run=NULL WHERE id=?", unretire)
    if scope == "full":
        con.execute(
            "UPDATE tests SET retired_run=:run WHERE retired_run IS NULL AND id != :coll "
            "AND id NOT IN (SELECT test_id FROM history WHERE run_id = :run) "
            "AND (last_run IS NULL OR (SELECT COALESCE(r.finished_at, 0), r.id FROM runs r "
            "     WHERE r.id = tests.last_run) < (:t, :run)) "
            "AND substr(test_id, 1, instr(test_id, '::') - 1) IN "
            "(SELECT DISTINCT substr(t2.test_id, 1, instr(t2.test_id, '::') - 1) "
            " FROM history h2 JOIN tests t2 ON t2.id = h2.test_id "
            " WHERE h2.run_id = :run AND t2.id != :coll)",
            {"run": run_id, "coll": coll_tid, "t": me[0]},
        )
    if outcomes:
        _retire_again(con, me, [tid for *_, tid in outcomes])
    if coll is not None:
        set_id, cur_set, deps, deps_t = coll
        if newer(deps, deps_t):  # the usual case: this run is the newest word
            if scope in ("full", "collect") or cur_set is None or cur_set == set_id:
                new_set = set_id
            else:
                new_set, _ = intern_set(con, set_members(con, cur_set) + set_members(con, set_id))
            con.execute(
                "UPDATE tests SET deps_set=?, deps_run=?, last_run=?, status='collection', "
                "mapped=1, first_run=COALESCE(first_run, ?) WHERE id=?",
                (new_set, run_id, run_id, run_id, coll_tid),
            )
        else:
            _refold_collection(con, coll_tid, run_id)


def _retire_again(con: sqlite3.Connection, me: tuple, tids: list[int]) -> None:
    """Tests this run just became the newest observation of, folded after a
    newer full run that observed their module without them: that run found
    them gone, and a late journal file must not bring them back."""
    newer_full = [r for (r,) in con.execute(
        "SELECT id FROM runs WHERE scope='full' AND lineage='own' "
        "AND (COALESCE(finished_at, 0), id) > (?, ?) "
        "ORDER BY COALESCE(finished_at, 0) DESC, id DESC", me,
    )]
    if not newer_full:
        return
    names = {}
    for tid in tids:
        names[tid] = con.execute("SELECT test_id FROM tests WHERE id=?", (tid,)).fetchone()[0]
    left = set(tids)
    for run_id in newer_full:  # newest first: the newest run to find a test gone retires it
        observed = {t for (t,) in con.execute(
            "SELECT test_id FROM history WHERE run_id=?", (run_id,))}
        modules = {name.split("::", 1)[0] for (name,) in con.execute(
            "SELECT t.test_id FROM history h JOIN tests t ON t.id = h.test_id "
            "WHERE h.run_id=? AND t.test_id LIKE '%::%'", (run_id,))}
        gone = [tid for tid in left
                if tid not in observed and names[tid].split("::", 1)[0] in modules]
        con.executemany("UPDATE tests SET retired_run=? WHERE id=?", [(run_id, t) for t in gone])
        left -= set(gone)


def _refold_collection(con: sqlite3.Connection, coll_tid: int, run_id: int) -> None:
    """The import-time set, recomputed from history when a run folds out of
    order: the newest complete snapshot (a full or collect-only run) unioned
    with every partial run after it, each of which only saw the import-time
    code of the modules it collected."""
    rows = con.execute(
        "SELECT r.id, r.scope, h.deps_set FROM history h JOIN runs r ON r.id = h.run_id "
        "WHERE h.test_id = ? AND r.lineage = 'own' AND h.deps_set IS NOT NULL "
        "ORDER BY COALESCE(r.finished_at, 0), r.id", (coll_tid,),
    ).fetchall()
    complete = [i for i, (_, scope, _) in enumerate(rows) if scope in ("full", "collect")]
    since = rows[complete[-1] if complete else 0:]
    sets = {set_id for _, _, set_id in since}
    new_set = next(iter(sets)) if len(sets) == 1 else intern_set(
        con, [f for set_id in sets for f in set_members(con, set_id)])[0]
    newest = since[-1][0]
    con.execute(
        "UPDATE tests SET deps_set=?, deps_run=?, last_run=?, status='collection', "
        "mapped=1, first_run=COALESCE(first_run, ?) WHERE id=?",
        (new_set, newest, newest, run_id, coll_tid),
    )


def rebuild_rollup(con: sqlite3.Connection) -> int:
    """Recompute the whole current view from history. Returns runs folded."""
    with con:
        con.execute(
            "UPDATE tests SET status=NULL, duration=NULL, mapped=NULL, deps_set=NULL, "
            "deps_run=NULL, first_run=NULL, last_run=NULL, retired_run=NULL, n_obs=0, "
            "n_failed=0"
        )
        run_ids = [r for (r,) in con.execute("SELECT id FROM runs ORDER BY id")]
        for run_id in run_ids:
            fold_run(con, run_id)
    return len(run_ids)


# --- provenance queries (what the selector reads) ----------------------------

def _run_dict(row) -> dict:
    d = dict(zip(RUN_COLUMNS, row))
    d["dirty_files"] = json.loads(d["dirty_files"] or "{}")
    d["args"] = json.loads(d["args"] or "[]")
    d["collect_errors"] = json.loads(d["collect_errors"] or "{}")
    d["tree_changed"] = bool(d["tree_changed"])
    return d


_BY_TIME = "ORDER BY COALESCE(finished_at, 0), id"


def runs(con: sqlite3.Connection, lineage: str | None = None) -> list[dict]:
    """Rolled-up runs in the order they happened, own and foreign unless
    `lineage` says which."""
    q = f"SELECT {', '.join(RUN_COLUMNS)} FROM runs"
    params: tuple = ()
    if lineage is not None:
        q += " WHERE lineage = ?"
        params = (lineage,)
    return [_run_dict(r) for r in con.execute(f"{q} {_BY_TIME}", params)]


def last_full_run(con: sqlite3.Connection) -> dict | None:
    """The newest own full run (a foreign one describes another branch)."""
    row = con.execute(
        f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE scope='full' AND lineage='own' "
        "ORDER BY COALESCE(finished_at, 0) DESC, id DESC LIMIT 1"
    ).fetchone()
    return _run_dict(row) if row else None


def contributing_runs(con: sqlite3.Connection) -> list[dict]:
    """Runs whose coverage evidence is still live: the last full run and every
    own coverage run after it (or every own coverage run, if no full run has
    been recorded), plus any older run that is still some live test's
    `deps_run` (a module that did not collect in the full run keeps its
    earlier evidence). Results-only and foreign runs carry no evidence to
    diff against."""
    full = last_full_run(con)
    q = f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE lineage='own' AND mode != 'results'"
    params: tuple = ()
    if full:
        q += " AND (COALESCE(finished_at, 0), id) >= (?, ?)"
        params = (full["finished_at"] or 0.0, full["id"])
    live = [_run_dict(r) for r in con.execute(f"{q} {_BY_TIME}", params)]
    ids = {r["id"] for r in live}
    older = [r for (r,) in con.execute(
        "SELECT DISTINCT deps_run FROM tests WHERE retired_run IS NULL AND deps_run IS NOT NULL"
    ) if r not in ids]
    if older:
        placeholders = ",".join("?" * len(older))
        extra = [_run_dict(r) for r in con.execute(
            f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE id IN ({placeholders})", older
        )]
        live = sorted(extra + live, key=lambda r: _order(r["finished_at"], r["id"]))
    return live


def blob(con: sqlite3.Connection, h: str | None) -> bytes | None:
    if h is None:
        return None
    row = con.execute("SELECT content FROM blobs WHERE hash=?", (h,)).fetchone()
    return row[0] if row else None


# --- migration ---------------------------------------------------------------

def _is_v1(con: sqlite3.Connection) -> bool:
    if not con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tests'"
    ).fetchone():
        return False
    cols = {r[1] for r in con.execute("PRAGMA table_info(tests)")}
    return "deps_set" not in cols


def _is_v2(con: sqlite3.Connection) -> bool:
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return "observations" in names and "history" not in names


def _is_v3(con: sqlite3.Connection) -> bool:
    cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    return "journal_key" in cols and "lineage" not in cols


def _migrate_v3(con: sqlite3.Connection) -> None:
    """v3 -> v4: runs carry their lineage; every v3 run was the checkout's own."""
    con.executescript(f"""
        BEGIN;
        ALTER TABLE runs ADD COLUMN lineage TEXT NOT NULL DEFAULT 'own';
        INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '{SCHEMA_VERSION}');
        COMMIT;
    """)


def _migrate_v2(con: sqlite3.Connection) -> None:
    """v2 (journal tables inside the map, folded by each writer) -> v3 (the map
    is the roll-up of journal files): observations become history, every v2
    run had coverage and counts as its own rollup. v3 -> v4 follows."""
    con.executescript("""
        BEGIN;
        ALTER TABLE runs ADD COLUMN journal_key TEXT;
        ALTER TABLE runs ADD COLUMN mode TEXT;
        ALTER TABLE runs ADD COLUMN worktree TEXT;
        ALTER TABLE runs ADD COLUMN collect_errors TEXT;
        ALTER TABLE runs ADD COLUMN rollup_seq INTEGER;
        UPDATE runs SET mode='coverage', rollup_seq=id, collect_errors='{}',
            worktree=(SELECT rtrim(value, '/') FROM meta WHERE key='rootpath');
        ALTER TABLE observations RENAME TO history;
        DROP INDEX IF EXISTS observations_by_test;
        ALTER TABLE tests ADD COLUMN deps_run INTEGER;
        UPDATE tests SET deps_run=last_run WHERE deps_set IS NOT NULL;
        COMMIT;
    """)
    con.executescript(SCHEMA)
    with con:
        last = con.execute("SELECT id, commit_sha FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            con.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                [("rollup_seq", str(last[0])), ("rollup_run", str(last[0])),
                 ("rollup_commit", last[1] or "")],
            )
        con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '3')")


def _migrate_v1(con: sqlite3.Connection) -> None:
    """v1 (one replaced-in-place map) -> the current schema: the old map
    becomes run #1, a full run of unknown commit, so nothing observed is
    lost."""
    con.executescript(
        "ALTER TABLE tests RENAME TO tests_v1; ALTER TABLE links RENAME TO links_v1;"
    )
    con.executescript(SCHEMA)
    with con:
        # when the v1 map was observed is unknown: before any run folded after
        # it, which the NULL finish time says (it orders first)
        run_id = con.execute(
            "INSERT INTO runs(started_at, finished_at, scope, mode, commit_sha, dirty_files, "
            "tree_changed, recorder, args, collect_errors, rollup_seq) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (None, None, "full", "coverage", None, "{}", 0, "migrated-from-schema-v1", "[]",
             "{}", 1),
        ).fetchone()[0]
        n = 0
        for tid_v1, test_id, duration, status in con.execute(
            "SELECT id, test_id, duration, status FROM tests_v1"
        ).fetchall():
            func_ids = [f for (f,) in con.execute(
                "SELECT func_id FROM links_v1 WHERE test_id=?", (tid_v1,)
            )]
            set_id, _ = intern_set(con, func_ids)
            con.execute(
                "INSERT INTO history(run_id, test_id, status, duration, deps_set) "
                "VALUES (?,?,?,?,?)",
                (run_id, intern_test(con, test_id), status, duration, set_id),
            )
            n += test_id != COLLECTION
        fold_run(con, run_id)
        con.execute(
            "UPDATE runs SET n_observed=?, n_collected=? WHERE id=?", (n, n, run_id)
        )
        con.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
            [("rollup_seq", "1"), ("rollup_run", str(run_id)), ("rollup_commit", "")],
        )
    con.executescript("DROP TABLE tests_v1; DROP TABLE links_v1;")
