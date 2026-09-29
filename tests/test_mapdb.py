"""Roll-up semantics, without pytest in the loop: runs are appended to the
journal as (filename, qualname, lineno) tuples and folded by rollup()."""

from __future__ import annotations

import os
import sqlite3

import pytest

from prongs import journal, mapdb
from prongs.mapdb import COLLECTION


@pytest.fixture
def root(tmp_path):
    return str(tmp_path) + os.sep


def record(db: str, root: str, records: dict, info: dict) -> dict:
    """One run: append it to the journal next to the map, then roll up."""
    jdir = os.path.join(os.path.dirname(db), "journal")
    journal.append(jdir, root, records, info)
    res = mapdb.rollup(db, jdir)
    (run,) = res["runs"]
    return run | {"n_funcs": res["n_funcs"]}


def dep(root, path, qual, line=1):
    return (root + path, qual, line)


def run_info(scope="full", commit="c0", dirty=None, **kw):
    return {
        "started_at": 1.0, "finished_at": 2.0, "scope": scope, "commit": commit,
        "dirty_files": dirty or {}, "tree_changed": False, "recorder": "test", "args": [],
        "n_collected": kw.get("n_collected"), "blobs": kw.get("blobs"),
    }


def links(con) -> set[tuple[str, str]]:
    return set(con.execute(
        "SELECT t.test_id, fn.qualname FROM current_links l JOIN tests t ON t.id=l.test_id "
        "JOIN funcs fn ON fn.id=l.func_id"
    ).fetchall())


def rows(con, sql):
    return con.execute(sql).fetchall()


def test_full_run_writes_journal_and_rollup(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    records = {
        COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")]),
        "tests/t.py::test_a": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
        "tests/t.py::test_b": (0.2, "failed", [dep(root, "pkg/mod.py", "b")]),
        "tests/t.py::test_b2": (0.2, "passed", [dep(root, "pkg/mod.py", "b")]),  # same deps as test_b
        "tests/t.py::test_ext": (0.3, "passed", [("/usr/lib/python3/site-packages/x.py", "f", 1)]),
    }
    res = record(db, root, records, run_info(n_collected=4))
    assert res["n_tests"] == 4 and res["run_id"] == 1
    assert res["n_new_sets"] == 4  # module, {a}, {b} (shared by test_b/test_b2), {} (ext)

    con = sqlite3.connect(db)
    assert rows(con, "SELECT scope, commit_sha, n_collected, n_observed FROM runs") == [
        ("full", "c0", 4, 4)
    ]
    assert rows(con, "SELECT COUNT(*) FROM history") == [(5,)]
    tests = {r[0]: r[1:] for r in rows(
        con, "SELECT test_id, status, mapped, first_run, last_run, retired_run, n_obs, n_failed FROM tests"
    )}
    assert tests["tests/t.py::test_a"] == ("passed", 1, 1, 1, None, 1, 0)
    assert tests["tests/t.py::test_b"] == ("failed", 1, 1, 1, None, 1, 1)
    assert tests["tests/t.py::test_ext"] == ("passed", 0, 1, 1, None, 1, 0)  # unmapped
    assert tests[COLLECTION][0] == "collection"
    # content addressing: test_b and test_b2 share one dependency set
    sets = dict(rows(con, "SELECT test_id, deps_set FROM tests"))
    assert sets["tests/t.py::test_b"] == sets["tests/t.py::test_b2"]
    assert links(con) == {
        (COLLECTION, "<module>"), ("tests/t.py::test_a", "a"),
        ("tests/t.py::test_b", "b"), ("tests/t.py::test_b2", "b"),
    }
    assert rows(con, "SELECT value FROM meta WHERE key='schema_version'") == [("4",)]


def test_partial_run_refreshes_only_what_it_observed(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    full = {
        COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")]),
        "tests/t.py::test_a": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
        "tests/t.py::test_b": (0.1, "passed", [dep(root, "pkg/mod.py", "b")]),
    }
    record(db, root, full, run_info())
    partial = {
        # a partial run only sees the import-time code of what it collected
        COLLECTION: (0.0, "collection", [dep(root, "pkg/other.py", "<module>")]),
        "tests/t.py::test_b": (0.1, "failed", [dep(root, "pkg/mod.py", "b"), dep(root, "pkg/mod.py", "c")]),
    }
    res = record(db, root, partial, run_info(scope="partial", commit="c1"))
    assert res["n_new_sets"] == 2  # observations: the other-module set and {b, c}

    con = sqlite3.connect(db)
    tests = {r[0]: r[1:] for r in rows(con, "SELECT test_id, status, last_run, n_obs FROM tests")}
    assert tests["tests/t.py::test_a"] == ("passed", 1, 1)  # untouched
    assert tests["tests/t.py::test_b"] == ("failed", 2, 2)
    assert links(con) >= {("tests/t.py::test_b", "b"), ("tests/t.py::test_b", "c"), ("tests/t.py::test_a", "a")}
    # collection set is unioned (a third, derived set), never replaced, by a partial run
    assert rows(con, "SELECT COUNT(*) FROM dep_sets") == [(6,)]
    coll_files = set(rows(con,
        "SELECT f.path FROM current_links l JOIN tests t ON t.id=l.test_id JOIN funcs fn ON fn.id=l.func_id "
        f"JOIN files f ON f.id=fn.file_id WHERE t.test_id='{COLLECTION}'"))
    assert coll_files == {("pkg/mod.py",), ("pkg/other.py",)}
    assert rows(con, "SELECT COUNT(*) FROM tests WHERE retired_run IS NOT NULL") == [(0,)]


def test_full_run_retires_missing_tests_in_collected_modules_only(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    first = {
        "tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
        "tests/a.py::test_2": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
        "tests/b.py::test_3": (0.1, "passed", [dep(root, "pkg/mod.py", "b")]),
    }
    record(db, root, first, run_info())
    # tests/a.py lost test_2; tests/b.py did not collect at all (import error)
    second = {"tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")])}
    record(db, root, second, run_info(commit="c1"))
    con = sqlite3.connect(db)
    retired = dict(rows(con, "SELECT test_id, retired_run FROM tests"))
    assert retired == {"tests/a.py::test_1": None, "tests/a.py::test_2": 2, "tests/b.py::test_3": None, COLLECTION: None}
    # the module that did not collect keeps its (older) evidence live
    assert [r["id"] for r in mapdb.contributing_runs(mapdb.connect(db))] == [1, 2]
    # observing a retired test again un-retires it
    record(db, root, {"tests/a.py::test_2": (0.1, "passed", [])}, run_info(scope="partial"))
    con = sqlite3.connect(db)
    assert dict(rows(con, "SELECT test_id, retired_run FROM tests"))["tests/a.py::test_2"] is None


def test_collect_only_run_never_retires(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    record(db, root, {"tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")])}, run_info())
    record(db, root, {COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")])},
                     run_info(scope="collect"))
    con = sqlite3.connect(db)
    assert rows(con, "SELECT retired_run FROM tests WHERE test_id='tests/a.py::test_1'") == [(None,)]
    assert {q for t, q in links(con) if t == COLLECTION} == {"<module>"}


def test_rebuild_rollup_reproduces_incremental_state(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    record(db, root, {
        COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")]),
        "tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
        "tests/a.py::test_2": (0.1, "passed", [dep(root, "pkg/mod.py", "b")]),
    }, run_info())
    record(db, root, {
        COLLECTION: (0.0, "collection", [dep(root, "pkg/x.py", "<module>")]),
        "tests/a.py::test_1": (0.1, "failed", [dep(root, "pkg/mod.py", "c")]),
    }, run_info(scope="partial"))
    record(db, root, {
        COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")]),
        "tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
    }, run_info(commit="c2"))
    con = mapdb.connect(db)
    before = rows(con, "SELECT * FROM tests ORDER BY id")
    assert mapdb.rebuild_rollup(con) == 3
    assert rows(con, "SELECT * FROM tests ORDER BY id") == before
    assert dict(rows(con, "SELECT test_id, retired_run FROM tests"))["tests/a.py::test_2"] == 3


def test_v1_map_is_migrated_into_run_one(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT UNIQUE);
        CREATE TABLE funcs (id INTEGER PRIMARY KEY, file_id INTEGER, qualname TEXT, lineno INTEGER,
                            UNIQUE(file_id, qualname, lineno));
        CREATE TABLE tests (id INTEGER PRIMARY KEY, test_id TEXT UNIQUE, duration REAL, status TEXT, mapped INTEGER);
        CREATE TABLE links (test_id INTEGER, func_id INTEGER, PRIMARY KEY (test_id, func_id)) WITHOUT ROWID;
        CREATE INDEX links_by_func ON links(func_id);
        INSERT INTO files VALUES (1, 'pkg/mod.py');
        INSERT INTO funcs VALUES (1, 1, 'a', 1), (2, 1, '<module>', 1);
        INSERT INTO tests VALUES (1, 'tests/a.py::test_1', 0.5, 'passed', 1), (2, '__collection__', 0, 'collection', 1),
                                 (3, 'tests/a.py::test_ext', 0.1, 'passed', 0);
        INSERT INTO links VALUES (1, 1), (2, 2);
    """)
    con.commit()
    con.close()

    con = mapdb.connect(db)
    assert rows(con, "SELECT scope, commit_sha, recorder, n_observed FROM runs") == [
        ("full", None, "migrated-from-schema-v1", 2)
    ]
    assert links(con) == {("tests/a.py::test_1", "a"), (COLLECTION, "<module>")}
    tests = {r[0]: r[1:] for r in rows(con, "SELECT test_id, status, mapped, duration, last_run FROM tests")}
    assert tests["tests/a.py::test_1"] == ("passed", 1, 0.5, 1)
    assert tests["tests/a.py::test_ext"] == ("passed", 0, 0.1, 1)
    assert not rows(con, "SELECT name FROM sqlite_master WHERE name IN ('tests_v1', 'links_v1')")
    # and the migrated store accepts new runs
    record(db, root, {"tests/a.py::test_1": (0.2, "failed", [dep(root, "pkg/mod.py", "b")])},
                     run_info(scope="partial", commit="c1"))
    con = sqlite3.connect(db)
    assert rows(con, "SELECT status, last_run FROM tests WHERE test_id='tests/a.py::test_1'") == [("failed", 2)]


def test_connect_does_not_write_when_the_schema_is_current(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    record(db, root, {"tests/a.py::t": (0.1, "passed", [])}, run_info())
    watcher = sqlite3.connect(db)
    version = watcher.execute("PRAGMA data_version").fetchone()
    for _ in range(3):
        con = mapdb.connect(db)  # a read path: must not take the write lock
        assert con.execute("SELECT COUNT(*) FROM runs").fetchone() == (1,)
        con.close()
    assert watcher.execute("PRAGMA data_version").fetchone() == version


def test_blobs_are_content_addressed(tmp_path, root):
    db = str(tmp_path / "map.sqlite")
    info = run_info(dirty={"pkg/mod.py": ["h1", "h1"]}, blobs={"h1": b"x = 1\n"})
    record(db, root, {"tests/a.py::t": (0.1, "passed", [])}, info)
    record(db, root, {"tests/a.py::t": (0.1, "passed", [])}, dict(info, scope="partial"))
    con = mapdb.connect(db)
    assert rows(con, "SELECT hash, content FROM blobs") == [("h1", b"x = 1\n")]
    assert mapdb.blob(con, "h1") == b"x = 1\n" and mapdb.blob(con, "nope") is None
    assert [r["dirty_files"] for r in mapdb.runs(con)] == [{"pkg/mod.py": ["h1", "h1"]}] * 2


def test_concurrent_writers_append_without_clobbering(tmp_path, root):
    db, jdir = str(tmp_path / "map.sqlite"), tmp_path / "journal"
    for i in range(3):  # three "workers", each with a partial view, each its own file
        journal.append(jdir, root, {
            COLLECTION: (0.0, "collection", [dep(root, f"pkg/m{i}.py", "<module>")]),
            f"tests/t.py::test_{i}": (0.1, "passed", [dep(root, f"pkg/m{i}.py", "f")]),
        }, run_info(scope="partial"))
    assert len(journal.pending(jdir)) == 3 and not os.path.exists(db)  # nothing wrote the map
    res = mapdb.rollup(db, jdir)
    assert (res["rolled_up"], res["rollup_seq"]) == (3, 1)  # one rollup, one transaction
    con = sqlite3.connect(db)
    assert rows(con, "SELECT COUNT(*), MIN(rollup_seq), MAX(rollup_seq) FROM runs") == [(3, 1, 1)]
    assert {q for t, q in links(con)} == {"<module>", "f"}
    coll = rows(con, "SELECT COUNT(*) FROM current_links l JOIN tests t ON t.id=l.test_id "
                     f"WHERE t.test_id='{COLLECTION}'")
    assert coll == [(3,)]  # all three import-time sets survived
    assert journal.pending(jdir) == [] and len(list((jdir / "consumed").iterdir())) == 3


def view(db: str) -> tuple[dict, dict]:
    """The current view with run ids replaced by journal keys, so maps folded
    in different orders compare: ({test_id: (status, deps, retired,
    last_run, deps_run)}, meta commit)."""
    con = sqlite3.connect(db)
    key = dict(rows(con, "SELECT id, journal_key FROM runs"))
    deps: dict[str, set] = {}
    for t, path, q in rows(con, "SELECT t.test_id, f.path, fn.qualname FROM current_links l "
                                "JOIN tests t ON t.id=l.test_id JOIN funcs fn ON fn.id=l.func_id "
                                "JOIN files f ON f.id=fn.file_id"):
        deps.setdefault(t, set()).add(q if path == "pkg/mod.py" else f"{path}:{q}")
    out = {
        t: (status, frozenset(deps.get(t, ())), retired is not None, key.get(last), key.get(dr))
        for t, status, retired, last, dr in rows(
            con, "SELECT test_id, status, retired_run, last_run, deps_run FROM tests")
    }
    return out, dict(rows(con, "SELECT key, value FROM meta WHERE key='rollup_commit'"))


def test_fold_order_does_not_change_the_map(tmp_path, root):
    # three runs, as three CI jobs' journal files; the second finished
    # between the others but may arrive last
    runs = [
        ({COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")]),
          "tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
          "tests/a.py::test_2": (0.1, "passed", [dep(root, "pkg/mod.py", "b")])},
         dict(run_info(commit="c1"), started_at=10.0, finished_at=11.0)),
        ({COLLECTION: (0.0, "collection", [dep(root, "pkg/x.py", "<module>")]),
          "tests/a.py::test_1": (0.1, "failed", [dep(root, "pkg/mod.py", "c")])},
         dict(run_info(scope="partial", commit="c2"), started_at=20.0, finished_at=21.0)),
        ({COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")]),
          "tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")])},
         dict(run_info(commit="c3"), started_at=30.0, finished_at=31.0)),
    ]
    views = []
    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0]):
        db, jdir = str(tmp_path / f"map-{order}.sqlite"), tmp_path / f"journal-{order}"
        keys = [f"2026010{i}T000000.000000Z-1-00000{i}" for i in range(3)]
        for i in order:  # one rollup per arrival
            journal.append(jdir, root, runs[i][0], runs[i][1], key=keys[i])
            mapdb.rollup(db, jdir)
        views.append(view(db))
    assert views[0] == views[1] == views[2]
    tests, commit = views[0]
    # the newest run wins: test_1 passes again on c3, test_2 was deleted by then
    assert tests["tests/a.py::test_1"][:3] == ("passed", frozenset({"a"}), False)
    assert tests["tests/a.py::test_2"][2] is True
    # the newest complete import-time snapshot replaced the partial run's union
    assert tests[COLLECTION][1] == {"<module>"}
    assert commit == {"rollup_commit": "c3"}
    # and replaying history in id order agrees with the incremental folds
    con = mapdb.connect(str(tmp_path / "map-[2, 0, 1].sqlite"))
    mapdb.rebuild_rollup(con)
    con.close()
    assert view(str(tmp_path / "map-[2, 0, 1].sqlite")) == views[0]


def test_a_foreign_run_adds_history_and_nothing_else(tmp_path, root):
    db, jdir = str(tmp_path / "map.sqlite"), tmp_path / "journal"
    record(db, root, {"tests/a.py::test_1": (0.1, "passed", [dep(root, "pkg/mod.py", "a")])},
           dict(run_info(commit="main1"), finished_at=10.0))
    # another branch's CI job, newer and full, failing test_1 and adding a test
    journal.append(jdir / journal.FOREIGN, root, {
        COLLECTION: (0.0, "collection", [dep(root, "pkg/branch.py", "<module>")]),
        "tests/a.py::test_1": (0.1, "failed", [dep(root, "pkg/mod.py", "z")]),
        "tests/a.py::test_new": (0.1, "passed", [dep(root, "pkg/mod.py", "a")]),
    }, dict(run_info(commit="pr1"), finished_at=20.0))
    res = mapdb.rollup(db, jdir)
    assert [r["lineage"] for r in res["runs"]] == ["foreign"] and res["commit"] == "main1"
    con = mapdb.connect(db)
    assert rows(con, "SELECT status, deps_run, last_run, retired_run FROM tests "
                     "WHERE test_id='tests/a.py::test_1'") == [("passed", 1, 1, None)]
    assert links(con) == {("tests/a.py::test_1", "a")}  # no foreign evidence
    assert rows(con, "SELECT last_run FROM tests WHERE test_id='tests/a.py::test_new'") == [(None,)]
    assert rows(con, "SELECT status, deps_set FROM history WHERE run_id=2 ORDER BY test_id") == [
        ("failed", None), ("passed", None)
    ]
    assert rows(con, "SELECT value FROM meta WHERE key='rollup_commit'") == [("main1",)]
    assert [r["id"] for r in mapdb.contributing_runs(con)] == [1]
    assert mapdb.last_full_run(con)["id"] == 1
    assert journal.pending_all(jdir) == []


def test_a_partial_run_after_the_last_full_run_stays_in_the_import_time_set(tmp_path, root):
    full = ({COLLECTION: (0.0, "collection", [dep(root, "pkg/mod.py", "<module>")])},
            dict(run_info(commit="c1"), finished_at=11.0))
    partial = ({COLLECTION: (0.0, "collection", [dep(root, "pkg/x.py", "<module>")])},
               dict(run_info(scope="partial", commit="c2"), finished_at=21.0))
    for order in ((full, partial), (partial, full)):
        db, jdir = str(tmp_path / f"map-{id(order)}.sqlite"), tmp_path / f"journal-{id(order)}"
        for records, info in order:
            journal.append(jdir, root, records, info)
            mapdb.rollup(db, jdir)
        con = sqlite3.connect(db)
        assert set(rows(con, "SELECT f.path FROM current_links l JOIN tests t ON t.id=l.test_id "
                             "JOIN funcs fn ON fn.id=l.func_id JOIN files f ON f.id=fn.file_id "
                             f"WHERE t.test_id='{COLLECTION}'")) == {("pkg/mod.py",), ("pkg/x.py",)}
