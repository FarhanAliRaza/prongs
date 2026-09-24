"""The recorder as a pytest plugin: scope classification and provenance,
observed through the journal it writes."""

from __future__ import annotations

import json

from conftest import T_A, T_B, T_C


def test_full_run_records_scope_commit_and_counts(recorded):
    (rid, scope, commit, dirty, changed, n_collected, n_observed), = recorded.runs()
    assert (rid, scope, commit, dirty, changed, n_collected, n_observed) == (
        1, "full", recorded.head(), "{}", 0, 3, 3
    )
    assert {t: v[1] for t, v in recorded.tests().items()} == {
        "__collection__": 1, T_A: 1, T_B: 1, T_C: 1
    }


def test_node_id_run_is_partial(recorded):
    assert recorded.record(T_A).returncode == 0
    assert recorded.runs()[-1][1:] == ("partial", recorded.head(), "{}", 0, 1, 1)
    assert all(v[2] is None for v in recorded.tests().values())  # nothing retired


def test_subdirectory_and_keyword_runs_are_partial(recorded):
    recorded.write("tests/unit/__init__.py", "")
    recorded.write("tests/unit/test_u.py", "def test_u():\n    assert True\n")
    recorded.commit("unit dir")
    assert recorded.record().returncode == 0  # full: covers testpaths
    assert recorded.record("tests/unit").returncode == 0
    assert recorded.record("-k", "test_a").returncode == 0
    assert [r[1] for r in recorded.runs()] == ["full", "full", "partial", "partial"]


def test_collect_only_run_records_collection_without_retiring(recorded):
    assert recorded.record("--collect-only").returncode == 0
    assert recorded.runs()[-1][1] == "collect" and recorded.runs()[-1][6] == 0
    assert all(v[2] is None for v in recorded.tests().values())


def test_interrupted_full_run_is_downgraded_to_partial(recorded):
    recorded.write("tests/test_0_first.py", "def test_boom():\n    assert False\n")
    p = recorded.record("-x")
    assert p.returncode == 1
    assert recorded.runs()[-1][1] == "partial"
    assert recorded.tests()[T_A][2] is None  # test_m never ran; not retired


def test_empty_session_is_never_a_full_run(recorded):
    (recorded.path / "tests/test_m.py").unlink()
    assert recorded.record().returncode == 5  # no tests collected
    assert recorded.runs()[-1][1] == "partial"
    assert all(v[2] is None for v in recorded.tests().values())


def test_dirty_files_are_snapshotted_with_content(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # dirty")
    recorded.write("tests/test_extra.py", "def test_x():\n    assert True\n")  # untracked
    assert recorded.record().returncode == 0
    run = recorded.runs()[-1]
    dirty = json.loads(run[3])
    assert set(dirty) == {"pkg/mod.py", "tests/test_extra.py"}
    assert all(start == end for start, end in dirty.values())
    con = recorded.db()
    blobs = dict(con.execute("SELECT hash, content FROM blobs"))
    assert blobs[dirty["pkg/mod.py"][0]] == recorded.read("pkg/mod.py").encode()
    assert run[4] == 0  # tree did not change during the run


def test_file_written_during_the_run_is_flagged(recorded):
    recorded.write(
        "tests/test_writer.py",
        "import pathlib\n\ndef test_w():\n    pathlib.Path('pkg/generated.py').write_text('X = 1\\n')\n",
    )
    recorded.commit("writer test")
    assert recorded.record().returncode == 0
    run = recorded.runs()[-1]
    assert run[4] == 1
    assert json.loads(run[3])["pkg/generated.py"][0] is None  # absent at start, present at end
    sel = recorded.select()
    assert "changed while it ran" in " ".join(sel["evidence"]["warnings"])
