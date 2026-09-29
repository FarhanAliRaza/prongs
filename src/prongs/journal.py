"""The journal: one SQLite file per recorded run, written once, never updated.

Every run appends — the recorder at the end of a pytest session, `prongs
run` for a run without coverage — and nothing but `prongs rollup` writes the
map (see mapdb.rollup). A journal file is the whole record of one run:

  run      one row: key, provenance (commit; the files that differed from
           HEAD, with content hashes at start and end; whether the tree
           changed mid-run), scope (full | partial | collect), mode (coverage
           | results), worktree, recorder fingerprint, args, counts, and the
           modules that failed to collect or were skipped at import
  blobs    content of the dirty .py files the run saw (content-addressed)
  funcs    project functions entered: (path relative to the worktree,
           qualname, first line)
  tests    one row per test: status, duration, and `mapped` — NULL in a
           results-only run, which carries outcomes and durations but no
           dependency evidence
  links    (test, func): what each test entered

A writer builds its file under a hidden temporary name and renames it into
place, so a reader never sees half a run and writers never contend: two
worktrees, xdist workers, a daemon child and a CI job each write their own
file. rollup folds pending files and moves them to consumed/; read_run() and
statuses() read one file with no map at all, which is all a CI artifact flow
needs. Files under journal/foreign/ were imported from another line of
history (`prongs ci restore`: a CI job whose commit is not an ancestor of
this checkout's HEAD) and fold as history only.

State lives in <worktree>/.prongs/ unless PRONGS_DIR says otherwise (point
several worktrees at one directory to pool their runs); PRONGS_JOURNAL
overrides just the journal directory.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
import time
from pathlib import Path

FORMAT = 1
COLLECTION = "__collection__"  # pseudo-test: code executed at import/collection time
STATE = ".prongs"
FOREIGN = "foreign"  # journal/foreign/: runs from another line of history

SCHEMA = """
CREATE TABLE run (
    key TEXT PRIMARY KEY, format INTEGER,
    started_at REAL, finished_at REAL,
    scope TEXT,              -- 'full' | 'partial' | 'collect'
    mode TEXT,               -- 'coverage' | 'results'
    commit_sha TEXT,         -- HEAD when the run started; NULL outside git
    worktree TEXT,           -- the checkout that ran it
    dirty_files TEXT,        -- JSON {path: [hash_at_start, hash_at_end]}
    tree_changed INTEGER,    -- HEAD moved or the dirty set changed mid-run
    recorder TEXT,           -- recorder fingerprint (prongs/python/pytest)
    args TEXT,               -- JSON: pytest invocation args
    n_collected INTEGER, n_observed INTEGER,
    collect_errors TEXT,     -- JSON {node id: last line of the error}
    collect_skipped TEXT     -- JSON [node id]: modules skipped at import
);
CREATE TABLE blobs (hash TEXT PRIMARY KEY, content BLOB);
CREATE TABLE funcs (
    id INTEGER PRIMARY KEY, path TEXT, qualname TEXT, lineno INTEGER,
    UNIQUE(path, qualname, lineno)
);
CREATE TABLE tests (
    id INTEGER PRIMARY KEY, test_id TEXT UNIQUE, status TEXT, duration REAL,
    mapped INTEGER           -- NULL: no coverage recorded for this test
);
CREATE TABLE links (test INTEGER, func INTEGER, PRIMARY KEY (test, func)) WITHOUT ROWID;
"""

RUN_FIELDS = (
    "key", "format", "started_at", "finished_at", "scope", "mode", "commit_sha", "worktree",
    "dirty_files", "tree_changed", "recorder", "args", "n_collected", "n_observed",
    "collect_errors", "collect_skipped",
)
_JSON_FIELDS = {"dirty_files": {}, "args": [], "collect_errors": {}, "collect_skipped": []}


# --- where state lives ---------------------------------------------------------

def state_dir(rootpath) -> Path:
    env = os.environ.get("PRONGS_DIR")
    return Path(rootpath) / env if env else Path(rootpath) / STATE


def journal_dir(rootpath) -> Path:
    env = os.environ.get("PRONGS_JOURNAL")
    return Path(rootpath) / env if env else state_dir(rootpath) / "journal"


def map_path(rootpath) -> Path:
    return state_dir(rootpath) / "map.sqlite"


def new_key(t: float | None = None) -> str:
    """Sortable, unique run key: UTC time, pid, randomness."""
    t = time.time() if t is None else t
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(t)) + f".{int(t % 1 * 1e6):06d}Z"
    return f"{stamp}-{os.getpid()}-{secrets.token_hex(3)}"


# --- writing -------------------------------------------------------------------

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


def _intern_func(con: sqlite3.Connection, funcs: dict, dep: tuple, rootpath: str) -> int | None:
    filename, qualname, lineno = dep
    if not is_project_file(filename, rootpath):
        return None
    k = (os.path.relpath(filename, rootpath), qualname, lineno)
    fid = funcs.get(k)
    if fid is None:
        fid = funcs[k] = con.execute(
            "INSERT INTO funcs(path, qualname, lineno) VALUES (?,?,?) RETURNING id", k,
        ).fetchone()[0]
    return fid


def append(jdir, rootpath: str, records: dict, run: dict, key: str | None = None) -> dict:
    """Write one run as one journal file and return {'key', 'path', 'mode',
    'n_tests', 'n_funcs', 'n_links'}.

    records: {test_id: (duration, status, deps)} where deps is an iterable of
             code objects or (filename, qualname, lineno) tuples, or None for
             a results-only observation; the COLLECTION pseudo-test carries
             import/collection-time code.
    run:     {'started_at', 'finished_at', 'scope', 'mode', 'commit',
              'worktree', 'dirty_files', 'blobs', 'tree_changed', 'recorder',
              'args', 'n_collected', 'collect_errors', 'collect_skipped'}
             where blobs is {hash: bytes} for the dirty files' observed
             content.
    """
    jdir = Path(jdir)
    jdir.mkdir(parents=True, exist_ok=True)
    key = key or new_key()
    final, tmp = jdir / f"{key}.sqlite", jdir / f".tmp-{key}.sqlite"
    mode = run.get("mode") or (
        "results" if records and all(d is None for _, _, d in records.values()) else "coverage"
    )
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode=OFF")  # a failed write is discarded whole
        con.execute("PRAGMA synchronous=OFF")
        con.executescript(SCHEMA)
        funcs: dict[tuple, int] = {}  # (relative path, qualname, lineno) -> id
        seen: dict = {}  # dependency as recorded -> id, or None when not project code
        links: list[tuple[int, int]] = []
        n_tests = 0
        with con:
            con.executemany(
                "INSERT OR IGNORE INTO blobs(hash, content) VALUES (?,?)",
                [(h, c) for h, c in (run.get("blobs") or {}).items() if c is not None],
            )
            for test_id, (duration, status, deps) in records.items():
                ids = None
                if deps is not None:
                    ids = set()
                    for dep in deps:  # the same code object recurs across tests: look it up first
                        fid = seen.get(dep, 0)
                        if fid == 0:
                            fid = seen[dep] = _intern_func(con, funcs, _dep_key(dep), rootpath)
                        if fid is not None:
                            ids.add(fid)
                tid = con.execute(
                    "INSERT INTO tests(test_id, status, duration, mapped) VALUES (?,?,?,?) "
                    "RETURNING id",
                    (test_id, status, duration, None if ids is None else int(bool(ids))),
                ).fetchone()[0]
                links.extend((tid, f) for f in ids or ())
                n_tests += test_id != COLLECTION
            con.executemany("INSERT INTO links(test, func) VALUES (?,?)", links)
            con.execute(
                f"INSERT INTO run({', '.join(RUN_FIELDS)}) VALUES ({', '.join('?' * len(RUN_FIELDS))})",
                (
                    key, FORMAT, run.get("started_at"), run.get("finished_at", time.time()),
                    run.get("scope", "partial"), mode, run.get("commit"),
                    run.get("worktree", rootpath.rstrip(os.sep)),
                    json.dumps(run.get("dirty_files") or {}, sort_keys=True),
                    int(bool(run.get("tree_changed"))), run.get("recorder"),
                    json.dumps(list(run.get("args") or [])), run.get("n_collected"), n_tests,
                    json.dumps(run.get("collect_errors") or {}, sort_keys=True),
                    json.dumps(sorted(run.get("collect_skipped") or [])),
                ),
            )
        con.close()
        os.replace(tmp, final)  # atomic: the run appears whole or not at all
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    return {"key": key, "path": str(final), "mode": mode, "n_tests": n_tests,
            "n_funcs": len(funcs), "n_links": len(links)}


# --- reading (stateless: no map involved) --------------------------------------

def open_ro(path) -> sqlite3.Connection:
    """A journal file is immutable once in place: read it without locking."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def run_row(con: sqlite3.Connection) -> dict:
    row = con.execute(f"SELECT {', '.join(RUN_FIELDS)} FROM run").fetchone()
    if row is None:
        raise sqlite3.DatabaseError("journal file has no run row")
    d = dict(zip(RUN_FIELDS, row))
    for f, empty in _JSON_FIELDS.items():
        d[f] = json.loads(d[f]) if d[f] else type(empty)()
    d["tree_changed"] = bool(d["tree_changed"])
    return d


def read_run(path) -> dict:
    con = open_ro(path)
    try:
        return run_row(con)
    finally:
        con.close()


def statuses(path) -> dict[str, str]:
    """{test_id: status} observed by one journal file."""
    con = open_ro(path)
    try:
        return {t: s for t, s in con.execute("SELECT test_id, status FROM tests") if t != COLLECTION}
    finally:
        con.close()


def pending(jdir) -> list[Path]:
    """Journal files in `jdir` not yet rolled up, by name (≈ finish time)."""
    jdir = Path(jdir)
    if not jdir.is_dir():
        return []
    return sorted(p for p in jdir.glob("*.sqlite") if not p.name.startswith("."))


def pending_all(jdir) -> list[Path]:
    """Every journal file a rollup would fold: own, then foreign."""
    return pending(jdir) + pending(Path(jdir) / FOREIGN)


def find(jdir, key: str) -> Path | None:
    """A run's journal file, pending (own or foreign) or already consumed."""
    for sub in ("", FOREIGN, "consumed"):
        p = Path(jdir) / sub / f"{key}.sqlite"
        if p.exists():
            return p
    return None


def _move(paths, dest: Path) -> None:
    if not paths:
        return
    dest.mkdir(parents=True, exist_ok=True)
    for p in paths:
        try:
            shutil.move(str(p), str(dest / Path(p).name))
        except FileNotFoundError:
            pass  # a concurrent rollup got there first


def consume(jdir, paths) -> None:
    _move(paths, Path(jdir) / "consumed")


def quarantine(jdir, paths) -> None:
    """Unreadable files (a writer killed mid-rename on a crashed disk, a file
    from a newer format) are set aside, never folded and never fatal."""
    _move(paths, Path(jdir) / "corrupt")
