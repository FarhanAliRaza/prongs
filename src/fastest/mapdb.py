"""SQLite storage for the per-test coverage map."""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS funcs (
    id INTEGER PRIMARY KEY, file_id INTEGER, qualname TEXT, lineno INTEGER,
    UNIQUE(file_id, qualname, lineno)
);
CREATE TABLE IF NOT EXISTS tests (
    id INTEGER PRIMARY KEY, test_id TEXT UNIQUE,
    duration REAL, status TEXT, mapped INTEGER
);
CREATE TABLE IF NOT EXISTS links (
    test_id INTEGER, func_id INTEGER,
    PRIMARY KEY (test_id, func_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS links_by_func ON links(func_id);
"""


def dump(path: str, rootpath: str, records: dict) -> tuple[int, int]:
    """records: {test_id: (duration, status, set[code])} -> (n_tests, n_funcs)"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("BEGIN")

    file_ids: dict[str, int] = {}
    func_ids: dict[tuple, int] = {}

    def file_id(f: str) -> int:
        fid = file_ids.get(f)
        if fid is None:
            rel = os.path.relpath(f, rootpath)
            cur = con.execute(
                "INSERT INTO files(path) VALUES(?) ON CONFLICT DO UPDATE SET path=path RETURNING id",
                (rel,),
            )
            fid = file_ids[f] = cur.fetchone()[0]
        return fid

    def func_id(code) -> int:
        key = (code.co_filename, code.co_qualname, code.co_firstlineno)
        fid = func_ids.get(key)
        if fid is None:
            cur = con.execute(
                "INSERT INTO funcs(file_id, qualname, lineno) VALUES(?,?,?) "
                "ON CONFLICT DO UPDATE SET qualname=qualname RETURNING id",
                (file_id(code.co_filename), code.co_qualname, code.co_firstlineno),
            )
            fid = func_ids[key] = cur.fetchone()[0]
        return fid

    n_tests = 0
    for test_id, (dur, status, codes) in records.items():
        # keep only project code: inside rootpath, not site-packages/stdlib
        project_codes = [
            c
            for c in codes
            if c.co_filename.startswith(rootpath)
            and "site-packages" not in c.co_filename
            and not c.co_filename.startswith("<")
        ]
        mapped = 1 if project_codes else 0
        cur = con.execute(
            "INSERT INTO tests(test_id, duration, status, mapped) VALUES(?,?,?,?) "
            "ON CONFLICT(test_id) DO UPDATE SET duration=excluded.duration, "
            "status=excluded.status, mapped=excluded.mapped RETURNING id",
            (test_id, dur, status, mapped),
        )
        tid = cur.fetchone()[0]
        con.execute("DELETE FROM links WHERE test_id=?", (tid,))
        con.executemany(
            "INSERT OR IGNORE INTO links(test_id, func_id) VALUES(?,?)",
            [(tid, func_id(c)) for c in project_codes],
        )
        n_tests += 1

    con.commit()
    n_funcs = con.execute("SELECT COUNT(*) FROM funcs").fetchone()[0]
    con.close()
    return n_tests, n_funcs
