"""Selection against real recorded runs in a throwaway git project.

The scenario mirrors an agent loop: full run, edit, selected run recorded,
edit again, commit, more edits. The property under test is the one from
notes/anthropic-tia-scaling.md: evidence never goes stale without the
selector knowing, and a recorded partial run is credited exactly."""

from __future__ import annotations

from conftest import T_A, T_B, T_C


def test_clean_tree_selects_nothing_and_reports_evidence(recorded):
    sel = recorded.select()
    ev = sel["evidence"]
    assert sel["mode"] == "select" and sel["targets"] == [] and sel["n_total"] == 3
    assert ev["base_source"] == "evidence"
    assert ev["evidence_commits"] == [recorded.head()] and ev["commits_behind"] == 0
    assert ev["last_full_run"]["scope"] == "full" and ev["last_full_run"]["n_observed"] == 3
    assert ev["trees"] == [{"commit": recorded.head()[:8], "dirty": [], "unknown": [], "runs": [1]}]
    assert ev["warnings"] == [] and ev["tests_refreshed_since_full"] == 0


def test_function_change_selects_its_dependents(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    sel = recorded.select()
    assert sel["mode"] == "select"
    assert sel["targets"] == [T_C, T_B]
    assert sel["changed_functions"] == ["pkg/mod.py::b"]
    assert sel["selected"][T_B].startswith("touches changed function pkg/mod.py::b")
    assert sel["n_skipped"] == 1


def test_recorded_partial_run_is_credited_exactly(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    out = recorded.cli("run")  # recording is the default
    s = out["summary"]
    assert s["status"] == "passed" and s["ran"] == 2 and s["complete"] is True
    assert s["unrun_targets"] == [] and s["record_ok"] is True
    key = s["recorded"].pop("journal")
    assert s["recorded"] == {"mode": "coverage", "scope": "partial", "n_observed": 2}
    assert recorded.journal_files() == [f"{key}.sqlite"]  # appended, not yet rolled up
    assert [r[1] for r in recorded.runs()] == ["full", "partial"]

    sel = recorded.select()
    ev = sel["evidence"]
    assert sel["targets"] == []  # run 2 saw exactly this content
    assert ev["dirty_rule"]["unchanged_since_observation"] == ["pkg/mod.py"]
    assert ev["tests_refreshed_since_full"] == 2 and ev["contributing_runs"] == [1, 2]
    assert [t["runs"] for t in ev["trees"]] == [[1], [2]]
    assert recorded.tests()[T_A][1] == 1 and recorded.tests()[T_B][1] == 2


def test_second_edit_is_line_diffed_against_observed_content(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    recorded.cli("run", "--record")
    recorded.edit("pkg/mod.py", "return 1", "return 1  # edited")
    sel = recorded.select()
    # test_a: evidence from run 1 (commit), diff commit..now touches a and b
    # test_c: evidence from run 2 (dirty blob), diff blob..now touches a only
    # test_b: evidence from run 2, and b is unchanged since -> skipped
    assert sel["mode"] == "select" and sel["targets"] == [T_C, T_A]
    assert sel["evidence"]["dirty_rule"] == {"unchanged_since_observation": [], "forced_wholesale": []}


def test_commit_after_partial_run_diffs_from_evidence_not_head(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    recorded.cli("run", "--record")
    recorded.edit("pkg/mod.py", "return 1", "return 1  # edited")
    recorded.commit("edit a and b")
    sel = recorded.select()
    ev = sel["evidence"]
    assert sel["targets"] == [T_C, T_A]
    assert ev["commits_behind"] == 1 and ev["head"] != ev["evidence_commits"][0]
    # once the new state is recorded, nothing is left to run
    recorded.cli("run", "--record")
    assert recorded.select()["targets"] == []
    assert recorded.select()["evidence"]["trees"][-1]["commit"] == recorded.head()[:8]


def test_new_tests_are_targeted_as_whole_files(recorded):
    recorded.write("tests/test_new.py", "def test_new():\n    assert True\n")  # untracked
    recorded.write("tests/test_m.py", recorded.read("tests/test_m.py") + "\ndef test_d():\n    assert True\n")
    sel = recorded.select()
    assert sel["mode"] == "select"
    assert sel["selected_files"] == {
        "tests/test_m.py": "1 test(s) not in the map: test_d",
        "tests/test_new.py": "1 test(s) not in the map: test_new",
    }
    assert sel["targets"] == ["tests/test_m.py", "tests/test_new.py"]
    assert set(sel["selected"]) == {T_A, T_B, T_C}  # subsumed by the file target


def test_test_module_changes_never_escalate_to_run_all(recorded):
    recorded.edit("tests/test_m.py", "def test_b():\n    assert b() == 2\n\n", "")  # delete a test
    sel = recorded.select()
    assert sel["mode"] == "select"
    # test_b's node id no longer exists, so the file is the target, not the ids
    assert sel["targets"] == ["tests/test_m.py"]
    assert sel["selected_files"] == {"tests/test_m.py": "1 known test(s) not found statically: test_b"}
    assert set(sel["selected"]) == {T_A, T_B, T_C}
    assert all(r.startswith("test file changed") for r in sel["selected"].values())
    # editing a test body selects that file's known tests: all of them here,
    # so pytest gets the file, not one id per test
    recorded.git("checkout", "--", "tests/test_m.py")
    recorded.edit("tests/test_m.py", "assert a() == 1", "assert a() == 1  # edited")
    sel = recorded.select()
    assert set(sel["selected"]) == {T_A, T_B, T_C} and sel["selected_files"] == {}
    assert sel["targets"] == ["tests/test_m.py"]


def test_import_time_change_in_production_code_runs_all(recorded):
    recorded.edit("pkg/mod.py", "def a():", "X = 1\ndef a():")
    sel = recorded.select()
    assert sel["mode"] == "run_all"
    assert sel["reasons"] == ["module-level change in import-time file: pkg/mod.py"]
    assert sel["targets"] == ["tests/test_m.py"] and sel["selected"][T_A] == "run_all"


def test_conftest_change_runs_all(recorded):
    recorded.write("tests/conftest.py", "import pytest\n")
    sel = recorded.select()
    assert sel["mode"] == "run_all" and sel["reasons"] == ["conftest changed: tests/conftest.py"]


def test_non_python_files_follow_the_tracked_untracked_policy(recorded):
    recorded.write("scratch.json", "{}")  # untracked, present at observation
    assert recorded.record().returncode == 0
    recorded.write("scratch.json", "{\"a\": 1}")  # changed since: still ignored
    sel = recorded.select()
    assert sel["mode"] == "select" and sel["targets"] == []
    assert "scratch.json" not in sel["evidence"]["dirty_rule"]["forced_wholesale"]
    con = recorded.db()
    assert con.execute("SELECT COUNT(*) FROM blobs").fetchone() == (0,)  # only .py content is kept
    recorded.write("pyproject.toml", recorded.read("pyproject.toml") + "# tracked change\n")
    sel = recorded.select()
    assert sel["mode"] == "run_all" and sel["reasons"] == ["non-Python file changed: pyproject.toml"]


def test_deleted_test_is_retired_by_the_next_full_run(recorded):
    recorded.edit("tests/test_m.py", "def test_b():\n    assert b() == 2\n\n", "")
    assert recorded.record().returncode == 0
    tests = recorded.tests()
    assert tests[T_B][2] == 2  # retired by run 2
    sel = recorded.select()
    assert sel["n_total"] == 2 and sel["targets"] == []
    assert sel["evidence"]["dirty_rule"]["unchanged_since_observation"] == ["tests/test_m.py"]


def test_deleted_module_is_excluded_from_selection(recorded):
    (recorded.path / "tests/test_m.py").unlink()
    sel = recorded.select()
    assert sel["n_total"] == 0 and sel["evidence"]["missing_modules"] == ["tests/test_m.py"]


def test_explicit_base_collapses_to_one_tree_and_warns_when_off_evidence(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    sel = recorded.select(base="HEAD")
    assert sel["targets"] == [T_C, T_B]
    assert sel["evidence"]["base_source"] == "explicit" and sel["evidence"]["warnings"] == []
    assert sel["evidence"]["trees"] == [{"commit": "HEAD", "dirty": [], "unknown": [], "runs": "all"}]
    recorded.commit("edit b")
    sel = recorded.select(base="HEAD~1")
    assert sel["targets"] == [T_C, T_B]
    assert sel["evidence"]["warnings"] == []  # HEAD~1 is the evidence commit
    sel = recorded.select(base="HEAD")
    assert sel["evidence"]["warnings"] and "not an evidence commit" in sel["evidence"]["warnings"][0]


def test_commit_to_commit_selection(recorded):
    """The replay harness diffs two commits with the map built at the first."""
    recorded.edit("pkg/mod.py", "return 1", "return 1  # edited")
    first = recorded.head()
    second = recorded.commit("edit a")
    sel = recorded.select(base=first, head=second)
    assert sel["targets"] == [T_C, T_A] and sel["changed_functions"] == ["pkg/mod.py::a"]


def test_no_record_leaves_the_journal_alone(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    out = recorded.cli("run", "--no-record")
    assert out["summary"]["ran"] == 2 and "recorded" not in out["summary"]
    assert [r[1] for r in recorded.runs()] == ["full"]


def test_targets_that_never_ran_are_reported(recorded):
    recorded.write(
        "tests/test_m.py",
        "import pytest\npytest.skip('whole module', allow_module_level=True)\n" + recorded.read("tests/test_m.py"),
    )
    out = recorded.cli("run", code=2)
    s = out["summary"]
    assert s["ran"] == 0 and s["failed"] == 0
    # the module is skipped at import, so pytest collects nothing: exit 5
    assert s["status"] == "error" and s["complete"] is False
    assert out["error"].startswith("pytest exit 5")
    assert out["collect_skipped"] == ["tests/test_m.py"]
    assert s["unrun_targets"] == ["tests/test_m.py"]
    assert out["conservation"]["conserved"] is False
    assert out["conservation"]["not_executed"] == sorted([T_A, T_B, T_C])
    assert s["record_ok"] is True  # the journal saw exactly what ran: nothing


def test_unknown_revision_is_a_clean_error(recorded):
    assert recorded.select(base="nope")["error"] == "unknown revision: nope"
    assert recorded.cli("affected", "--base", "nope", code=2)["error"] == "unknown revision: nope"


def test_non_git_directory_is_a_clean_error(tmp_path):
    from prongs.select import select

    assert select(tmp_path / "map.sqlite", tmp_path)["mode"] == "error"
    assert not (tmp_path / "map.sqlite").exists()  # nothing was created


def test_unittest_style_classes_are_found_statically(recorded):
    recorded.write(
        "tests/test_u.py",
        "import unittest\nfrom pkg.mod import a\n\nclass ModTests(unittest.TestCase):\n"
        "    def test_a(self):\n        self.assertEqual(a(), 1)\n",
    )
    recorded.commit("unittest style")
    assert recorded.record().returncode == 0
    recorded.edit("tests/test_u.py", "assertEqual(a(), 1)", "assertEqual(a(), 1)  # edited")
    sel = recorded.select()
    assert sel["selected"] == {"tests/test_u.py::ModTests::test_a": "test file changed: tests/test_u.py"}
    assert sel["targets"] == ["tests/test_u.py"] and sel["selected_files"] == {}


def test_skip_receipt_is_tied_to_the_evidence(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    out = recorded.cli("affected")
    receipt = out["skip_receipt"]
    assert receipt["skipped"] == 1 and receipt["evidence"]["runs"] == [1]
    assert receipt["evidence"]["commits"] == [recorded.head()[:8]]
    assert receipt["evidence"]["last_full_run"] == 1 and receipt["warnings"] == []
    recorded.write("tests/conftest.py", "import pytest\n")
    assert recorded.cli("affected")["skip_receipt"]["skipped"] == 0


def test_cli_affected_reports_evidence(recorded):
    out = recorded.cli("affected")
    assert out["mode"] == "select" and out["targets"] == []
    assert out["evidence"]["last_full_run"]["id"] == 1 and out["evidence"]["commits_behind"] == 0
    assert "skip_receipt" in out
