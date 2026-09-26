"""`fastest audit`: the selection checked against a full run of the same
tree, in a real project. A status change the selection skipped is a miss;
re-run alone, it is either first-order (still changed) or pollution."""

from __future__ import annotations

import sys

from fastest.audit import audit, run_alone


def test_audit_passes_when_every_status_change_was_selected(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 3")  # breaks test_b and test_c
    out = recorded.cli("audit")
    assert out["verdict"] == "pass" and out["misses"] == [] and out["pollution"] == []
    assert out["status_changes"] == {"total": 2, "selected": 2, "unselected": 0}
    assert out["selection"]["n_selected"] == 2 and out["full_run"]["n_results"] == 3
    assert out["baseline"] == {"source": "map", "n_tests": 3}
    assert "statuses" not in out
    assert [r[1] for r in recorded.runs()] == ["full"]  # an audit never feeds the map


def test_audit_reports_a_first_order_miss_with_its_receipt(recorded):
    # a dependency the map cannot see: an untracked data file, which the
    # selector ignores by policy
    recorded.write(
        "tests/test_data.py",
        "import pathlib\n\n"
        "def test_data():\n"
        "    assert pathlib.Path(__file__).with_name('data.txt').read_text() == 'ok'\n",
    )
    recorded.commit("data test")
    recorded.write("tests/data.txt", "ok")
    assert recorded.record().returncode == 0
    recorded.write("tests/data.txt", "changed")
    out = recorded.cli("audit", "--base", "HEAD")
    assert out["selection"]["n_selected"] == 0 and out["verdict"] == "miss"
    (miss,) = out["misses"]
    assert miss["test"] == "tests/test_data.py::test_data"
    assert (miss["before"], miss["after"], miss["alone"]) == ("passed", "failed", "failed")
    receipt = miss["receipt"]
    assert receipt["rule"].startswith("no overlap") and receipt["observed_by_run"] == 2
    assert receipt["n_deps"] >= 1 and receipt["deps_in_changed_files"] == []


def test_audit_separates_pollution_from_misses(recorded):
    recorded.edit("pkg/mod.py", "def a():", "STATE = {'ok': True}\n\n\ndef a():")
    recorded.write(
        "tests/test_p.py",
        "from pkg import mod\n\n"
        "def test_1_leaves_state_behind_on_failure():\n"
        "    mod.STATE['ok'] = False\n    assert mod.a() == 1\n    mod.STATE['ok'] = True\n\n"
        "def test_2_reads_state():\n    assert mod.STATE['ok']\n",
    )
    recorded.commit("stateful tests")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "return 1", "return 5")
    out = recorded.cli("audit")
    # test_a, test_c and test_1 touch a() and were selected; test_2 only
    # fails in-suite, after test_1 skipped its cleanup
    assert out["status_changes"] == {"total": 4, "selected": 3, "unselected": 1}
    assert out["verdict"] == "pass" and out["misses"] == []
    (p,) = out["pollution"]
    assert p["test"] == "tests/test_p.py::test_2_reads_state"
    assert (p["before"], p["after"], p["alone"]) == ("passed", "failed", "passed")


def test_audit_record_appends_the_full_run(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    out = recorded.cli("audit", "--record")
    assert out["verdict"] == "pass"
    assert recorded.journal_files() == [out["full_run"]["journal"] + ".sqlite"]  # pending
    assert [r[1] for r in recorded.runs()] == ["full", "full"]


def test_a_control_run_separates_environment_drift_from_misses(recorded):
    # a test that passes only on a fresh checkout: the state it leaves behind
    # sits in an ignored directory no diff and no coverage map can see
    recorded.write(".gitignore", ".fastest/\n__pycache__/\nstate/\n")
    recorded.write(
        "tests/test_once.py",
        "import pathlib\n\n"
        "def test_once():\n"
        "    p = pathlib.Path('state/ran')\n"
        "    first = not p.exists()\n"
        "    p.parent.mkdir(exist_ok=True)\n"
        "    p.touch()\n"
        "    assert first\n",
    )
    recorded.commit("a run-once test")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    assert recorded.cli("audit")["misses"][0]["test"] == "tests/test_once.py::test_once"

    def control(tests):  # the same tests, alone, on the unedited code
        recorded.git("stash", "-q")
        try:
            return run_alone(recorded.path, sys.executable, (), tests)
        finally:
            recorded.git("stash", "pop", "-q")

    out = audit(recorded.path, control=control)
    assert out["verdict"] == "pass" and out["misses"] == []
    (d,) = out["drift"]
    assert (d["test"], d["before"], d["after"], d["alone"], d["on_base"]) == (
        "tests/test_once.py::test_once", "passed", "failed", "failed", "failed"
    )
