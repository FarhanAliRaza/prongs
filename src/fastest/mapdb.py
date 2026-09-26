"""SQLite storage for the per-test dependency map: the roll-up of the journal.

Runs are recorded as journal files (journal.py): append-only, one file per
run, any number of writers. map.sqlite is derived from them by rollup(), the
only writer of the map, and is read by the selector:

  runs          one row per rolled-up run: its journal key, provenance (commit,
                the files that differed from HEAD with content hashes at start
                and end of the run, recorder fingerprint), scope, mode,
                worktree, counts, and the rollup that folded it
  history       one row per (run, test): outcome, duration, and the dependency
                set when the run recorded coverage (NULL when it did not) —
                the per-test result history
  dep_sets      content-addressed sets of project functions, shared between
  dep_members   history rows, so an unchanged dependency set costs one row
  blobs         content of files that were dirty at observation
  tests         current view per test: latest outcome and duration, the
                dependency set and the run it came from (`deps_run`: the tree
                that evidence was observed on), first/last run, counters,
                retirement
  current_links view over tests x dep_members: the inverted map
  meta          schema version; the last rollup's sequence number, run and
                commit

Interned: files, funcs ((file, qualname, lineno) -> int).

rollup() folds every pending journal file in one write transaction, then
moves the files to journal/consumed/. Runs are keyed by their journal key, so
a rollup interrupted between its commit and the move never folds a run twice;
concurrent rollups serialize on the write lock and readers are never blocked
(WAL). fold_run() is the one fold, used by rollup() and, from history alone,
by rebuild_rollup().
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import time
from pathlib import Path

from fastest import journal

SCHEMA_VERSION = 3
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
    recorder TEXT,           -- recorder fingerprint (fastest/python/pytest)
    args TEXT,               -- JSON: pytest invocation args
    n_collected INTEGER, n_observed INTEGER,
    collect_errors TEXT,     -- JSON {node id: last line of the error}
    rollup_seq INTEGER       -- the rollup that folded it
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
    "n_observed", "collect_errors", "rollup_seq",
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

def rollup(path, jdir) -> dict:
    """Fold every pending journal file into the map in one write transaction,
    in the order the runs finished; then record the rolled-up commit and run
    in meta and move the files to consumed/. With nothing pending the map is
    not even opened.

    Returns {'rolled_up', 'rollup_seq', 'runs': [{'run_id', 'key', 'mode',
    'scope', 'n_tests', 'n_new_sets'}], 'already_folded', 'corrupt',
    'commit', 'n_funcs', 'wall_s'}."""
    t0 = time.monotonic()
    out: dict = {"rolled_up": 0, "rollup_seq": None, "runs": [], "already_folded": 0,
                 "corrupt": [], "commit": None}
    if not journal.pending(jdir):
        return out | {"wall_s": round(time.monotonic() - t0, 3)}
    con = connect(str(path))
    folded, done, bad = [], [], []
    try:
        con.execute("BEGIN IMMEDIATE")  # one rollup at a time
        entries = []
        for f in journal.pending(jdir):  # listed again under the lock
            try:
                run = journal.read_run(f)
            except (sqlite3.DatabaseError, ValueError, FileNotFoundError):
                bad.append(f)
                continue
            if run["format"] != journal.FORMAT:
                bad.append(f)
                continue
            entries.append((run["finished_at"] or 0.0, run["key"], f, run))
        entries.sort(key=lambda e: e[:2])
        seq = int(meta(con).get("rollup_seq") or 0) + 1
        for _, key, f, run in entries:
            if con.execute("SELECT 1 FROM runs WHERE journal_key=?", (key,)).fetchone():
                done.append(f)  # folded by a rollup that died before moving it
                continue
            src = journal.open_ro(f)
            try:
                folded.append((f, run, _fold_file(con, src, run, seq)))
            finally:
                src.close()
        if folded:
            _, last_run, last = folded[-1]
            con.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)",
                [("rollup_seq", str(seq)), ("rollup_run", str(last["run_id"])),
                 ("rollup_commit", last_run["commit_sha"] or ""),
                 ("rolled_up_at", repr(time.time())),
                 ("rootpath", (last_run["worktree"] or "") + os.sep)],
            )
            out["rollup_seq"], out["commit"] = seq, last_run["commit_sha"]
        out["n_funcs"] = con.execute("SELECT COUNT(*) FROM funcs").fetchone()[0]
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()
    journal.consume(jdir, [f for f, _, _ in folded] + done)
    journal.quarantine(jdir, bad)
    out.update(
        rolled_up=len(folded), runs=[stats for _, _, stats in folded],
        already_folded=len(done), corrupt=[Path(f).name for f in bad],
        wall_s=round(time.monotonic() - t0, 3),
    )
    return out


def _fold_file(con: sqlite3.Connection, src: sqlite3.Connection, run: dict, seq: int) -> dict:
    """Append one journal file's run and history rows, then fold it."""
    run_id = con.execute(
        "INSERT INTO runs(journal_key, started_at, finished_at, scope, mode, commit_sha, "
        "worktree, dirty_files, tree_changed, recorder, args, n_collected, n_observed, "
        "collect_errors, rollup_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (
            run["key"], run["started_at"], run["finished_at"], run["scope"], run["mode"],
            run["commit_sha"], run["worktree"], json.dumps(run["dirty_files"], sort_keys=True),
            int(run["tree_changed"]), run["recorder"], json.dumps(run["args"]),
            run["n_collected"], run["n_observed"],
            json.dumps(run["collect_errors"], sort_keys=True), seq,
        ),
    ).fetchone()[0]
    con.executemany(
        "INSERT OR IGNORE INTO blobs(hash, content) VALUES (?,?)",
        src.execute("SELECT hash, content FROM blobs"),
    )
    interner = _Interner(con)
    func_ids = {
        jid: interner.func_id(p, q, ln)
        for jid, p, q, ln in src.execute("SELECT id, path, qualname, lineno FROM funcs")
    }
    deps: dict[int, list[int]] = {}
    for t, f in src.execute("SELECT test, func FROM links"):
        deps.setdefault(t, []).append(func_ids[f])
    rows, n_tests, n_new = [], 0, 0
    for jid, test_id, status, duration, mapped in src.execute(
        "SELECT id, test_id, status, duration, mapped FROM tests"
    ):
        set_id = None
        if mapped is not None:  # coverage recorded: the test's dependency set
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
            "n_tests": n_tests, "n_new_sets": n_new}


def fold_run(con: sqlite3.Connection, run_id: int) -> None:
    """Fold one run's history rows into the current view. Runs must be folded
    in id order; rebuild_rollup() replays them all through this function.

    Rules:
      * an observed test takes the run's outcome and duration and is
        un-retired; its dependency set, and the run that set came from
        (`deps_run`), change only when the run recorded coverage — a
        results-only observation never moves a test's evidence to a tree
        whose dependencies nobody observed;
      * a 'full' run retires tests that were not observed although their module
        was (deleted/renamed tests); tests in modules that did not collect at
        all keep their last evidence, which is what surfaces the breakage;
      * the collection pseudo-test is replaced by a full/collect run and
        unioned by a partial run, since a partial run only sees the import-time
        code of the modules it collected.
    """
    scope = con.execute("SELECT scope FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    coll_tid = intern_test(con, COLLECTION)
    rows = con.execute(
        "SELECT h.test_id, h.status, h.duration, h.deps_set, s.size "
        "FROM history h LEFT JOIN dep_sets s ON s.id=h.deps_set WHERE h.run_id=?",
        (run_id,),
    ).fetchall()
    coll_set = None
    for tid, status, duration, set_id, size in rows:
        if tid == coll_tid:
            coll_set = set_id
            continue
        if set_id is None:
            con.execute(
                "UPDATE tests SET status=?, duration=?, last_run=?, retired_run=NULL, "
                "first_run=COALESCE(first_run, ?), n_obs=n_obs+1, n_failed=n_failed+? "
                "WHERE id=?",
                (status, duration, run_id, run_id, int(status == "failed"), tid),
            )
            continue
        con.execute(
            "UPDATE tests SET status=?, duration=?, mapped=?, deps_set=?, deps_run=?, "
            "last_run=?, retired_run=NULL, first_run=COALESCE(first_run, ?), "
            "n_obs=n_obs+1, n_failed=n_failed+? WHERE id=?",
            (status, duration, int(size > 0), set_id, run_id, run_id, run_id,
             int(status == "failed"), tid),
        )
    if scope == "full":
        con.execute(
            "UPDATE tests SET retired_run=? WHERE retired_run IS NULL AND id != ? "
            "AND (last_run IS NULL OR last_run != ?) "
            "AND substr(test_id, 1, instr(test_id, '::') - 1) IN "
            "(SELECT DISTINCT substr(test_id, 1, instr(test_id, '::') - 1) "
            " FROM tests WHERE last_run = ?)",
            (run_id, coll_tid, run_id, run_id),
        )
    if coll_set is not None:
        new_set = coll_set
        if scope not in ("full", "collect"):
            current = con.execute(
                "SELECT deps_set FROM tests WHERE id=?", (coll_tid,)
            ).fetchone()[0]
            if current is not None and current != coll_set:
                new_set, _ = intern_set(
                    con, set_members(con, current) + set_members(con, coll_set)
                )
        con.execute(
            "UPDATE tests SET deps_set=?, deps_run=?, last_run=?, status='collection', "
            "mapped=1, first_run=COALESCE(first_run, ?) WHERE id=?",
            (new_set, run_id, run_id, run_id, coll_tid),
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


def runs(con: sqlite3.Connection, since_id: int | None = None) -> list[dict]:
    q = f"SELECT {', '.join(RUN_COLUMNS)} FROM runs"
    params: tuple = ()
    if since_id is not None:
        q += " WHERE id >= ?"
        params = (since_id,)
    return [_run_dict(r) for r in con.execute(q + " ORDER BY id", params)]


def last_full_run(con: sqlite3.Connection) -> dict | None:
    row = con.execute(
        f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE scope='full' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _run_dict(row) if row else None


def contributing_runs(con: sqlite3.Connection) -> list[dict]:
    """Runs whose coverage evidence is still live: the last full run and every
    coverage run after it (or every coverage run, if no full run has been
    recorded), plus any older run that is still some live test's `deps_run`
    (a module that did not collect in the full run keeps its earlier
    evidence). Results-only runs carry no evidence to diff against."""
    full = last_full_run(con)
    live = [r for r in runs(con, since_id=full["id"] if full else None) if r["mode"] != "results"]
    ids = {r["id"] for r in live}
    older = [r for (r,) in con.execute(
        "SELECT DISTINCT deps_run FROM tests WHERE retired_run IS NULL "
        "AND deps_run IS NOT NULL AND deps_run < ?", (min(ids) if ids else 0,)
    )]
    if older:
        placeholders = ",".join("?" * len(older))
        extra = [_run_dict(r) for r in con.execute(
            f"SELECT {', '.join(RUN_COLUMNS)} FROM runs WHERE id IN ({placeholders})", older
        )]
        live = sorted(extra + live, key=lambda r: r["id"])
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


def _migrate_v2(con: sqlite3.Connection) -> None:
    """v2 (journal tables inside the map, folded by each writer) -> v3 (the map
    is the roll-up of journal files): observations become history, every v2
    run had coverage and counts as its own rollup."""
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
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def _migrate_v1(con: sqlite3.Connection) -> None:
    """v1 (one replaced-in-place map) -> v3: the old map becomes run #1, a
    full run of unknown commit, so nothing observed is lost."""
    con.executescript(
        "ALTER TABLE tests RENAME TO tests_v1; ALTER TABLE links RENAME TO links_v1;"
    )
    con.executescript(SCHEMA)
    now = time.time()
    with con:
        run_id = con.execute(
            "INSERT INTO runs(started_at, finished_at, scope, mode, commit_sha, dirty_files, "
            "tree_changed, recorder, args, collect_errors, rollup_seq) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
            (now, now, "full", "coverage", None, "{}", 0, "migrated-from-schema-v1", "[]",
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
