"""`fastest run` as the agent reads it: the status verdict, the conservation
block that must balance, and failures grouped by root cause."""

from __future__ import annotations

from conftest import T_A, T_B, T_C

from fastest.__main__ import exception_line, group_failures


def test_selected_run_balances_its_books(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    out = recorded.cli("run")
    assert out["summary"]["status"] == "passed"
    assert out["conservation"] == {
        "collected": 3, "selected": 2, "run_all": 0, "skipped": 1, "executed": 2, "conserved": True,
    }


def test_run_all_is_its_own_bucket(recorded):
    recorded.write("tests/conftest.py", "import pytest\n")
    out = recorded.cli("run")
    assert out["selection"]["mode"] == "run_all" and out["summary"]["status"] == "passed"
    assert out["conservation"] == {
        "collected": 3, "selected": 0, "run_all": 3, "skipped": 0, "executed": 3, "conserved": True,
    }


def test_nothing_to_run_still_balances(recorded):
    out = recorded.cli("run")
    assert out["summary"]["status"] == "nothing_to_run"
    assert out["conservation"]["conserved"] is True and out["conservation"]["skipped"] == 3


def test_collection_failure_is_an_error_not_a_pass(recorded):
    recorded.edit("tests/test_m.py", "import a, b, c", "import a, b, c, missing")
    out = recorded.cli("run", code=2)
    s = out["summary"]
    assert s["status"] == "error" and s["ran"] == 0 and s["complete"] is False
    # 2 (collection interrupted) or 4 (its node ids cannot be resolved)
    assert out["error"].split(":")[0] in ("pytest exit 2", "pytest exit 4")
    (err,) = out["collect_errors"]
    assert err["id"] == "tests/test_m.py" and "ImportError" in err["longrepr"]
    assert out["conservation"]["conserved"] is False
    assert out["conservation"]["n_not_executed"] == 3


def test_tests_that_silently_did_not_run_make_the_run_inconsistent(recorded):
    # pytest exits 0 and nothing failed, but a selected test never ran
    recorded.write(
        "tests/conftest.py",
        "def pytest_collection_modifyitems(config, items):\n"
        "    items[:] = [i for i in items if i.name != 'test_b']\n",
    )
    out = recorded.cli("run", code=3)
    assert out["selection"]["mode"] == "run_all"
    assert out["summary"]["status"] == "inconsistent" and "error" not in out
    c = out["conservation"]
    assert (c["run_all"], c["executed"], c["conserved"], c["not_executed"]) == (3, 2, False, [T_B])


def test_without_a_map_everything_runs_and_the_run_builds_the_map(repo):
    # a first CI run, an expired cache: no evidence, so nothing may be skipped
    out = repo.cli("affected")
    assert out["mode"] == "run_all"
    assert out["run_all_reasons"] == ["no coverage map (this run records one)"]
    out = repo.cli("run")
    assert out["selection"]["mode"] == "run_all" and out["summary"]["status"] == "passed"
    assert out["summary"]["ran"] == 3 and out["summary"]["recorded"]["scope"] == "full"
    assert out["conservation"]["census"] == "no map: the run's own collection"
    # the next command rolls that run up and selects precisely
    repo.edit("pkg/mod.py", "return 2", "return 2  # edited")
    out = repo.cli("affected")
    assert out["mode"] == "select" and out["tests"] == sorted([T_B, T_C])


def test_without_a_map_a_failure_still_fails_the_run(repo):
    repo.edit("pkg/mod.py", "return 1", "return 5")
    out = repo.cli("run", code=1)
    assert out["summary"]["status"] == "failed" and out["summary"]["failed"] == 2


def test_run_all_runs_new_test_files_and_counts_them(recorded):
    recorded.write("tests/test_new.py", "def test_new():\n    assert True\n")
    recorded.write("tests/conftest.py", "import pytest\n")
    out = recorded.cli("run")
    assert out["selection"]["mode"] == "run_all"
    assert out["summary"]["status"] == "passed" and out["summary"]["ran"] == 4
    assert out["conservation"]["conserved"] is True and out["conservation"]["unplanned"] == 1


def test_deleted_test_is_expected_not_to_run(recorded):
    recorded.edit("tests/test_m.py", "def test_b():\n    assert b() == 2\n\n", "")
    out = recorded.cli("run")
    assert out["summary"]["status"] == "passed" and out["summary"]["ran"] == 2
    c = out["conservation"]
    assert c["conserved"] is True and c["vanished"] == 1 and c["executed"] == 2


def test_failures_are_grouped_by_exception_line(recorded):
    # --tb=short ends every truncated assertion diff with the same
    # 'Use -v to get more diff' hint: grouping by the last line merged them
    recorded.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "--tb=short"\n',
    )
    recorded.write(
        "tests/test_lists.py",
        "from pkg.mod import a\n\n"
        "def test_l1():\n    assert list(range(a(), a() + 10)) == list(range(1, 11))\n\n"
        "def test_l2():\n    assert [2 * x for x in range(a(), a() + 10)] == [2 * x for x in range(1, 11)]\n",
    )
    recorded.commit("tb=short and list tests")
    assert recorded.record().returncode == 0
    recorded.edit("pkg/mod.py", "return 1", "return 5")
    out = recorded.cli("run", code=1)
    assert out["summary"]["failed"] == 4
    reps = {g["representative"] for g in out["failures"]}
    assert reps == {T_A, T_C, "tests/test_lists.py::test_l1", "tests/test_lists.py::test_l2"}
    assert all("Use -v" not in g["error"] for g in out["failures"])
    # one root cause, four tests: one group
    recorded.edit("pkg/mod.py", "return 5", "raise RuntimeError('boom')")
    out = recorded.cli("run", code=1)
    (g,) = out["failures"]
    assert g["error"] == "RuntimeError: boom" and len(g["also_failed"]) == 3


def test_exception_line_skips_pytest_hints():
    rep = (
        ">       assert [1] * 9 == [2] * 9\n"
        "E       AssertionError: assert [1, 1, 1, 1, 1, 1, ...] == [2, 2, 2, 2, 2, 2, ...]\n"
        "E         At index 0 diff: 1 != 2\n"
        "E         Use -v to get more diff\n"
    )
    assert exception_line({"longrepr": rep}) == (
        "AssertionError: assert [1, 1, 1, 1, 1, 1, ...] == [2, 2, 2, 2, 2, 2, ...]"
    )
    assert exception_line({"longrepr": rep, "crash": "boom"}) == "boom"
    chained = (
        "E   KeyError: 'a'\n\nDuring handling of the above exception, another exception occurred:\n\n"
        "E   ValueError: bad object at 0x7f3a2b10\n"
    )
    assert exception_line({"longrepr": chained}) == "ValueError: bad object at 0x?"
    assert exception_line({"longrepr": "[XPASS(strict)] no longer fails"}) == (
        "[XPASS(strict)] no longer fails"
    )


def test_group_failures_keeps_one_traceback_per_root_cause():
    fs = [
        {"id": "t1", "crash": "RuntimeError: boom", "longrepr": "tb1"},
        {"id": "t2", "crash": "RuntimeError: boom", "longrepr": "tb2"},
        {"id": "t3", "crash": "KeyError: 'id'", "longrepr": "tb3"},
    ]
    assert [(g["error"], g["representative"], g["also_failed"]) for g in group_failures(fs)] == [
        ("RuntimeError: boom", "t1", ["t2"]), ("KeyError: 'id'", "t3", []),
    ]


def test_a_crash_in_pytest_start_up_is_an_error(recorded):
    # pytest-django runs django.setup() in this hook: a broken model import
    # escapes pytest.main() itself, with no exit code at all
    recorded.write(
        "crashplugin.py",
        "def pytest_load_initial_conftests(early_config, parser, args):\n"
        "    raise RuntimeError('broken app setup')\n",
    )
    recorded.write(
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\naddopts = "-p crashplugin"\n',
    )
    out = recorded.cli("run", code=2)
    assert out["summary"]["status"] == "error" and out["summary"]["ran"] == 0
    assert out["error"] == "pytest crashed before running tests: RuntimeError: broken app setup"
    assert "broken app setup" in out["pytest_output"]
    audit = recorded.cli("audit", code=2)  # the full run exits 1, like a test failure
    assert audit["verdict"] == "error" and audit["misses"] == []
    assert audit["error"].startswith("full run recorded nothing")
