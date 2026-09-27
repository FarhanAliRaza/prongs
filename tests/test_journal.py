"""The journal and the roll-up: every run appends one file, nothing but
rollup writes the map, and concurrent writers never contend."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys

from conftest import T_A, T_B, T_C, Repo

from fastest import journal, mapdb


def test_a_recorded_session_appends_a_file_and_never_writes_the_map(repo):
    assert repo.record().returncode == 0
    (name,) = repo.journal_files()
    assert not journal.map_path(repo.path).exists()  # the recorder wrote no map
    # the stateless reader needs no map at all
    path = journal.journal_dir(repo.path) / name
    run = journal.read_run(path)
    assert (run["scope"], run["mode"], run["commit_sha"]) == ("full", "coverage", repo.head())
    assert run["worktree"] == str(repo.path) and run["n_observed"] == 3
    assert journal.statuses(path) == {T_A: "passed", T_B: "passed", T_C: "passed"}

    res = repo.rollup()
    assert (res["rolled_up"], res["rollup_seq"], res["commit"]) == (1, 1, repo.head())
    assert repo.journal_files() == [] and repo.journal_files("consumed") == [name]
    con = repo.db()
    meta = dict(con.execute("SELECT key, value FROM meta"))
    assert (meta["rollup_seq"], meta["rollup_run"], meta["rollup_commit"]) == ("1", "1", repo.head())
    assert con.execute("SELECT journal_key, mode, worktree FROM runs").fetchall() == [
        (name.removesuffix(".sqlite"), "coverage", str(repo.path))
    ]
    assert repo.rollup()["rolled_up"] == 0  # nothing pending: the map is not touched


def test_rollup_never_folds_a_run_twice(recorded):
    recorded.rollup()
    (name,) = recorded.journal_files("consumed")
    jdir = journal.journal_dir(recorded.path)
    # a rollup that committed and died before moving its files
    shutil.copy(jdir / "consumed" / name, jdir / name)
    res = recorded.rollup()
    assert (res["rolled_up"], res["already_folded"]) == (0, 1)
    assert len(recorded.runs()) == 1 and recorded.journal_files() == []


def test_history_keeps_a_row_per_test_per_run(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    recorded.cli("run")
    recorded.rollup()
    con = recorded.db()
    rows = con.execute(
        "SELECT r.rollup_seq, t.test_id, h.status FROM history h JOIN tests t ON t.id=h.test_id "
        "JOIN runs r ON r.id=h.run_id WHERE t.test_id != '__collection__' ORDER BY h.run_id, t.test_id"
    ).fetchall()
    assert rows == [
        (1, T_C, "passed"), (1, T_A, "passed"), (1, T_B, "passed"),
        (2, T_C, "passed"), (2, T_B, "passed"),
    ]


def test_a_run_without_coverage_still_appends_outcomes(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 3")  # breaks test_b and test_c
    out = recorded.cli("run", "--no-cov", code=1)
    s = out["summary"]
    assert s["status"] == "failed" and s["record_ok"] is True
    assert s["recorded"]["mode"] == "results" and s["recorded"]["n_observed"] == 2
    (name,) = recorded.journal_files()
    src = journal.open_ro(journal.journal_dir(recorded.path) / name)
    assert src.execute("SELECT COUNT(*) FROM links").fetchone() == (0,)
    assert src.execute("SELECT test_id, status, mapped FROM tests ORDER BY test_id").fetchall() == [
        (T_C, "failed", None), (T_B, "failed", None),
    ]
    # rolled up: outcomes and durations land, the dependency evidence does not
    recorded.rollup()
    con = recorded.db()
    assert con.execute(
        "SELECT test_id, status, last_run, deps_run FROM tests WHERE test_id IN (?, ?) "
        "ORDER BY test_id", (T_B, T_C),
    ).fetchall() == [(T_C, "failed", 2, 1), (T_B, "failed", 2, 1)]
    assert con.execute("SELECT COUNT(*) FROM history WHERE run_id=2 AND deps_set IS NULL").fetchone() == (2,)
    # nobody observed test_b's dependencies on the edited tree, so it is still selected
    assert recorded.select()["targets"] == [T_C, T_B]


def test_a_test_without_dependency_evidence_is_unmapped_and_always_runs(repo):
    # test_b observed with no coverage (as a results-only run observes it):
    # it has no evidence to be skipped on, so it must never drop out
    root = str(repo.path) + os.sep
    dep = (root + "pkg/mod.py", "a", 1)
    journal.append(journal.journal_dir(repo.path), root, {
        T_A: (0.1, "passed", [dep]), T_B: (0.1, "passed", None), T_C: (0.1, "passed", [dep]),
    }, {"scope": "full", "mode": "coverage", "commit": repo.head()})
    sel = repo.select()
    assert sel["targets"] == [T_B] and sel["selected"][T_B] == "unmapped test (conservative)"


def test_two_worktrees_share_one_journal_without_contention(tmp_path, repo, monkeypatch):
    shared = tmp_path / "shared-state"
    monkeypatch.setenv("FASTEST_DIR", str(shared))
    repo.git("worktree", "add", "-q", str(tmp_path / "wt2"))
    one, two = Repo(repo.path, {"FASTEST_DIR": str(shared)}), Repo(tmp_path / "wt2", {"FASTEST_DIR": str(shared)})
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", FASTEST_DIR=str(shared))
    env.pop("PYTEST_ADDOPTS", None)
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "pytest", "--fastest-cov", "-q", "-p", "no:cacheprovider"],
            cwd=r.path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for r in (one, two)
    ]
    outs = [p.communicate() for p in procs]
    assert [p.returncode for p in procs] == [0, 0], outs
    assert not any("locked" in o + e for o, e in outs)
    assert len(one.journal_files()) == 2  # one file per session, same directory
    res = one.rollup()
    assert (res["rolled_up"], res["rollup_seq"]) == (2, 1)
    con = sqlite3.connect(shared / "map.sqlite")
    assert sorted(w for (w,) in con.execute("SELECT worktree FROM runs")) == sorted(
        [str(repo.path), str(tmp_path / "wt2")]
    )
    # either worktree selects from the pooled map
    assert two.select()["n_total"] == 3 and two.select()["targets"] == []


def test_an_unreadable_journal_file_is_quarantined(tmp_path):
    jdir = tmp_path / "journal"
    jdir.mkdir()
    (jdir / "20260101T000000.000000Z-1-abcdef.sqlite").write_bytes(b"not a database")
    res = mapdb.rollup(tmp_path / "map.sqlite", jdir)
    assert res["rolled_up"] == 0 and res["corrupt"] == ["20260101T000000.000000Z-1-abcdef.sqlite"]
    assert journal.pending(jdir) == [] and len(list((jdir / "corrupt").iterdir())) == 1


def test_affected_rolls_up_lazily_and_reports_the_journal(recorded):
    out = recorded.cli("affected", "--no-rollup")
    # no map until a rollup: nothing may be skipped, and the receipt says why
    assert out["mode"] == "run_all" and out["evidence"]["journal"]["pending"] == 1
    assert out["run_all_reasons"] == ["no coverage map (1 journal file(s) pending: roll them up)"]
    out = recorded.cli("affected")
    assert out["evidence"]["journal"] == {"rolled_up_now": 1, "pending": 0}
    assert out["evidence"]["rollup"] == {"seq": 1, "run": 1, "commit": recorded.head()}
    assert recorded.cli("rollup")["rolled_up"] == 0


def test_fastest_state_is_never_part_of_the_observed_tree(repo):
    repo.write(".gitignore", "__pycache__/\n")  # a project that does not ignore .fastest/
    repo.commit("stop ignoring .fastest")
    assert repo.record().returncode == 0
    repo.rollup()
    assert repo.record().returncode == 0  # the journal and map now exist, untracked
    assert [json.loads(r[3]) for r in repo.runs()] == [{}, {}]


V2_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT UNIQUE);
CREATE TABLE funcs (id INTEGER PRIMARY KEY, file_id INTEGER, qualname TEXT, lineno INTEGER,
                    UNIQUE(file_id, qualname, lineno));
CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL, finished_at REAL, scope TEXT,
                   commit_sha TEXT, dirty_files TEXT, tree_changed INTEGER, recorder TEXT,
                   args TEXT, n_collected INTEGER, n_observed INTEGER);
CREATE TABLE blobs (hash TEXT PRIMARY KEY, content BLOB);
CREATE TABLE dep_sets (id INTEGER PRIMARY KEY, hash BLOB UNIQUE, size INTEGER);
CREATE TABLE dep_members (set_id INTEGER, func_id INTEGER, PRIMARY KEY (set_id, func_id)) WITHOUT ROWID;
CREATE INDEX dep_members_by_func ON dep_members(func_id);
CREATE TABLE tests (id INTEGER PRIMARY KEY, test_id TEXT UNIQUE, status TEXT, duration REAL,
                    mapped INTEGER, deps_set INTEGER, first_run INTEGER, last_run INTEGER,
                    retired_run INTEGER, n_obs INTEGER NOT NULL DEFAULT 0,
                    n_failed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE observations (run_id INTEGER, test_id INTEGER, status TEXT, duration REAL,
                           deps_set INTEGER, PRIMARY KEY (run_id, test_id)) WITHOUT ROWID;
CREATE INDEX observations_by_test ON observations(test_id);
CREATE VIEW current_links AS SELECT t.id AS test_id, m.func_id FROM tests t
    JOIN dep_members m ON m.set_id = t.deps_set WHERE t.retired_run IS NULL;
INSERT INTO meta VALUES ('schema_version', '2'), ('rootpath', '/proj/');
INSERT INTO files VALUES (1, 'pkg/mod.py');
INSERT INTO funcs VALUES (1, 1, 'a', 1);
INSERT INTO runs VALUES (1, 1.0, 2.0, 'full', 'c0', '{}', 0, 'test', '[]', 1, 1),
                        (2, 3.0, 4.0, 'partial', 'c1', '{}', 0, 'test', '[]', 1, 1);
INSERT INTO dep_sets VALUES (1, x'00', 1);
INSERT INTO dep_members VALUES (1, 1);
INSERT INTO tests VALUES (1, 'tests/a.py::test_1', 'failed', 0.5, 1, 1, 1, 2, NULL, 2, 1),
                         (2, '__collection__', 'collection', NULL, 1, 1, 1, 1, NULL, 0, 0);
INSERT INTO observations VALUES (1, 1, 'passed', 0.4, 1), (1, 2, 'collection', 0.0, 1),
                                (2, 1, 'failed', 0.5, 1);
"""


def test_v2_map_is_migrated_with_its_history(tmp_path):
    db = tmp_path / "map.sqlite"
    con = sqlite3.connect(db)
    con.executescript(V2_SCHEMA)
    con.close()
    con = mapdb.connect(str(db))
    assert con.execute("SELECT id, mode, worktree, rollup_seq FROM runs").fetchall() == [
        (1, "coverage", "/proj", 1), (2, "coverage", "/proj", 2)
    ]
    assert con.execute("SELECT run_id, status FROM history WHERE test_id=1").fetchall() == [
        (1, "passed"), (2, "failed")
    ]
    assert con.execute("SELECT deps_run, last_run FROM tests").fetchall() == [(2, 2), (1, 1)]
    assert mapdb.meta(con)["schema_version"] == "3" and mapdb.meta(con)["rollup_seq"] == "2"
    before = con.execute("SELECT * FROM tests").fetchall()
    assert mapdb.rebuild_rollup(con) == 2 and con.execute("SELECT * FROM tests").fetchall() == before
    con.close()
    # and the migrated map accepts rolled-up journal files
    root = str(tmp_path) + os.sep
    journal.append(tmp_path / "journal", root, {"tests/a.py::test_1": (0.1, "passed", [])},
                   {"scope": "partial", "commit": "c2"})
    assert mapdb.rollup(db, tmp_path / "journal")["runs"][0]["run_id"] == 3
