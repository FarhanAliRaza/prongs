"""SQLite storage for the per-test dependency map.

Two halves, per the listener / roll-up split in notes/anthropic-tia-scaling.md.

Journal — append-only, any writer, never updated after the fact:
  runs          one row per recorded pytest session: provenance (commit, the
                files that differed from HEAD with content hashes at start and
                end of the run, recorder fingerprint), scope, timing, counts
  observations  one row per (run, test): outcome, duration, dependency set
  dep_sets      content-addressed sets of project functions, shared between
  dep_members   observations, so an unchanged dependency set costs one row

Roll-up — derived from the journal by fold_run(), rebuildable from scratch
with rebuild_rollup():
  tests         current view per test: latest observation, dependency set,
                first/last run, counters, retirement
  current_links view over tests x dep_members: the inverted map the selector
                reads. Nothing but the fold reads observations.

Interned: files, funcs ((file, qualname, lineno) -> int).

Concurrency: WAL plus one write transaction per run. Several writers (agent
worktrees, xdist workers, a daemon child and a CI job) serialize on the
SQLite write lock and never clobber each other, because a run only ever
appends and the fold is idempotent per run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import time

SCHEMA_VERSION = 2
COLLECTION = "__collection__"  # pseudo-test: code executed at import/collection time

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
    started_at REAL, finished_at REAL,
    scope TEXT,              -- 'full' | 'partial' | 'collect'
    commit_sha TEXT,         -- HEAD when the run started; NULL outside git
    dirty_files TEXT,        -- JSON {path: [hash_at_start, hash_at_end]}
    tree_changed INTEGER,    -- HEAD moved or the dirty set changed mid-run
    recorder TEXT,           -- recorder fingerprint (fastest/python/pytest)
    args TEXT,               -- JSON: pytest invocation args
    n_collected INTEGER, n_observed INTEGER
);
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
    status TEXT, duration REAL, mapped INTEGER, deps_set INTEGER,
    first_run INTEGER, last_run INTEGER, retired_run INTEGER,
    n_obs INTEGER NOT NULL DEFAULT 0, n_failed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS tests_by_set ON tests(deps_set);
CREATE TABLE IF NOT EXISTS observations (
    run_id INTEGER, test_id INTEGER, status TEXT, duration REAL, deps_set INTEGER,
    PRIMARY KEY (run_id, test_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS observations_by_test ON observations(test_id);
CREATE VIEW IF NOT EXISTS current_links AS
    SELECT t.id AS test_id, m.func_id
    FROM tests t JOIN dep_members m ON m.set_id = t.deps_set
    WHERE t.retired_run IS NULL;
"""

RUN_COLUMNS = (
    "id", "started_at", "finished_at", "scope", "commit_sha", "dirty_files",
    "tree_changed", "recorder", "args", "n_collected", "n_observed",
)


def connect(path: str) -> sqlite3.Connection:
    """Open (creating or migrating as needed) and return a connection."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    if _is_v1(con):
        _migrate_v1(con)
    con.executescript(SCHEMA)
    con.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    con.commit()
    return con


# --- interning ---------------------------------------------------------------

def is_project_file(filename: str, rootpath: str) -> bool:
    return (
        filename.startswith(rootpath)
        and "site-packages" not in filename
        and not filename.startswith("<")
    )


def _dep_key(dep) -> tuple[str, str, int]:
    """Accept code objects (the recorder) or (filename, qualname, lineno) tuples."""
    if hasattr(dep, "co_filename"):
        return (dep.co_filename, dep.co_qualname, dep.co_firstlineno)
    return tuple(dep)  # type: ignore[return-value]


class _Interner:
    def __init__(self, con: sqlite3.Connection, rootpath: str):
        self.con, self.rootpath = con, rootpath
        self._files: dict[str, int] = {}
        self._funcs: dict[tuple, int] = {}

    def file_id(self, filename: str) -> int:
        fid = self._files.get(filename)
        if fid is None:
            rel = os.path.relpath(filename, self.rootpath)
            fid = self._files[filename] = self.con.execute(
                "INSERT INTO files(path) VALUES(?) "
                "ON CONFLICT DO UPDATE SET path=path RETURNING id",
                (rel,),
            ).fetchone()[0]
        return fid

    def func_id(self, key: tuple[str, str, int]) -> int:
        fid = self._funcs.get(key)
        if fid is None:
            filename, qualname, lineno = key
            fid = self._funcs[key] = self.con.execute(
                "INSERT INTO funcs(file_id, qualname, lineno) VALUES(?,?,?) "
                "ON CONFLICT DO UPDATE SET qualname=qualname RETURNING id",
                (self.file_id(filename), qualname, lineno),
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


# --- journal -----------------------------------------------------------------

def record_run(path: str, rootpath: str, records: dict, run: dict) -> dict:
    """Append one run and its observations, then fold it into the roll-up.

    records: {test_id: (duration, status, deps)} where deps is an iterable of
             code objects or (filename, qualname, lineno) tuples; the
             COLLECTION pseudo-test carries import/collection-time code.
    run:     {'started_at', 'finished_at', 'scope', 'commit', 'dirty_files',
              'blobs', 'tree_changed', 'recorder', 'args', 'n_collected'}
             where blobs is {hash: bytes} for the dirty files' observed content.
    Returns {'run_id', 'n_tests', 'n_funcs', 'n_new_sets'}.
    """
    con = connect(path)
    try:
        with con:
            con.executemany(
                "INSERT OR IGNORE INTO blobs(hash, content) VALUES (?,?)",
                [(h, c) for h, c in (run.get("blobs") or {}).items() if c is not None],
            )
            run_id = con.execute(
                "INSERT INTO runs(started_at, finished_at, scope, commit_sha, dirty_files, "
                "tree_changed, recorder, args, n_collected) VALUES (?,?,?,?,?,?,?,?,?) "
                "RETURNING id",
                (
                    run.get("started_at"), run.get("finished_at", time.time()),
                    run.get("scope", "partial"), run.get("commit"),
                    json.dumps(run.get("dirty_files") or {}, sort_keys=True),
                    int(bool(run.get("tree_changed"))), run.get("recorder"),
                    json.dumps(list(run.get("args") or [])), run.get("n_collected"),
                ),
            ).fetchone()[0]
            interner = _Interner(con, rootpath)
            n_tests = n_new_sets = 0
            for test_id, (duration, status, deps) in records.items():
                keys = (_dep_key(d) for d in deps)
                func_ids = [
                    interner.func_id(k) for k in keys if is_project_file(k[0], rootpath)
                ]
                set_id, created = intern_set(con, func_ids)
                n_new_sets += created
                con.execute(
                    "INSERT INTO observations(run_id, test_id, status, duration, deps_set) "
                    "VALUES (?,?,?,?,?)",
                    (run_id, intern_test(con, test_id), status, duration, set_id),
                )
                n_tests += test_id != COLLECTION
            fold_run(con, run_id)
            con.execute("UPDATE runs SET n_observed=? WHERE id=?", (n_tests, run_id))
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('rootpath', ?)", (rootpath,)
            )
            n_funcs = con.execute("SELECT COUNT(*) FROM funcs").fetchone()[0]
    finally:
        con.close()
    return {"run_id": run_id, "n_tests": n_tests, "n_funcs": n_funcs, "n_new_sets": n_new_sets}


# --- roll-up -----------------------------------------------------------------

def fold_run(con: sqlite3.Connection, run_id: int) -> None:
    """Fold one run's observations into the current view. Runs must be folded
    in id order; rebuild_rollup() replays them all through this same function.

    Rules:
      * an observed test takes the run's outcome, duration and dependency set,
        and is un-retired;
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
        "SELECT o.test_id, o.status, o.duration, o.deps_set, s.size "
        "FROM observations o JOIN dep_sets s ON s.id=o.deps_set WHERE o.run_id=?",
        (run_id,),
    ).fetchall()
    coll_set = None
    for tid, status, duration, set_id, size in rows:
        if tid == coll_tid:
            coll_set = set_id
            continue
        con.execute(
            "UPDATE tests SET status=?, duration=?, mapped=?, deps_set=?, last_run=?, "
            "retired_run=NULL, first_run=COALESCE(first_run, ?), n_obs=n_obs+1, "
            "n_failed=n_failed+? WHERE id=?",
            (status, duration, int(size > 0), set_id, run_id, run_id,
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
            "UPDATE tests SET deps_set=?, last_run=?, status='collection', mapped=1, "
            "first_run=COALESCE(first_run, ?) WHERE id=?",
            (new_set, run_id, run_id, coll_tid),
        )


def rebuild_rollup(con: sqlite3.Connection) -> int:
    """Recompute the whole current view from the journal. Returns runs folded."""
    with con:
        con.execute(
            "UPDATE tests SET status=NULL, duration=NULL, mapped=NULL, deps_set=NULL, "
            "first_run=NULL, last_run=NULL, retired_run=NULL, n_obs=0, n_failed=0"
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
    """Runs whose evidence is still live: the last full run and everything
    after it (or every run, if no full run has been recorded), plus any older
    run that is still the last observation of a live test (a module that did
    not collect in the full run keeps its earlier evidence)."""
    full = last_full_run(con)
    live = runs(con, since_id=full["id"] if full else None)
    ids = {r["id"] for r in live}
    older = [r for (r,) in con.execute(
        "SELECT DISTINCT last_run FROM tests WHERE retired_run IS NULL "
        "AND last_run IS NOT NULL AND last_run < ?", (min(ids) if ids else 0,)
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


def _migrate_v1(con: sqlite3.Connection) -> None:
    """v1 (one replaced-in-place map) -> v2: the old map becomes run #1, a
    full run of unknown commit, so nothing observed is lost."""
    con.executescript(
        "ALTER TABLE tests RENAME TO tests_v1; ALTER TABLE links RENAME TO links_v1;"
    )
    con.executescript(SCHEMA)
    now = time.time()
    with con:
        run_id = con.execute(
            "INSERT INTO runs(started_at, finished_at, scope, commit_sha, dirty_files, "
            "tree_changed, recorder, args) VALUES (?,?,?,?,?,?,?,?) RETURNING id",
            (now, now, "full", None, "{}", 0, "migrated-from-schema-v1", "[]"),
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
                "INSERT INTO observations(run_id, test_id, status, duration, deps_set) "
                "VALUES (?,?,?,?,?)",
                (run_id, intern_test(con, test_id), status, duration, set_id),
            )
            n += test_id != COLLECTION
        fold_run(con, run_id)
        con.execute(
            "UPDATE runs SET n_observed=?, n_collected=? WHERE id=?", (n, n, run_id)
        )
    con.executescript("DROP TABLE tests_v1; DROP TABLE links_v1;")
