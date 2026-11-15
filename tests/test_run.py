"""`prongs run` as the agent reads it: the status verdict, the conservation
block that must balance, and failures grouped by root cause."""

from __future__ import annotations

import re

import pytest
from conftest import MOD, T_A, T_B, T_C

from prongs.__main__ import exception_line, group_failures


def test_selected_run_balances_its_books(recorded):
    recorded.edit("pkg/mod.py", "return 2", "return 2 * 1")
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
    repo.edit("pkg/mod.py", "return 2", "return 2 * 1")
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


def test_a_crash_after_the_tests_ran_is_an_error_that_keeps_their_results(recorded):
    # httpx under filterwarnings=error: resources a broken __exit__ leaked
    # surface at the very end as an ExceptionGroup out of pytest.main()
    recorded.write("tests/conftest.py",
                   "def pytest_unconfigure(config):\n    raise RuntimeError('late')\n")
    out = recorded.cli("run", code=2)
    assert out["error"] == "pytest crashed after 3 test(s) ran: RuntimeError: late"
    assert out["summary"]["status"] == "error" and out["summary"]["ran"] == 3


def test_stop_without_a_daemon_reports_it(recorded):
    out = recorded.cli("stop")
    assert out == {"stopped": False, "reason": "no daemon running"}


def test_run_args_reach_a_cold_run(recorded):
    # an option pytest rejects: the run must fail as an error, proving the
    # arguments reached the in-process pytest, not just the daemon's children
    recorded.env["PRONGS_RUN_ARGS"] = "--no-such-option"
    recorded.edit("pkg/mod.py", "return 2", "return 2 * 1")
    out = recorded.cli("run", code=2)
    assert out["summary"]["status"] == "error"


SHARED = "SEEN = []\n\n\ndef reset():\n    SEEN.clear()\n"
POLLUTED = (
    "from pkg import shared\nfrom pkg.mod import a\n\n\n"
    "def test_polluter():\n    shared.SEEN.append(a())\n\n\n"
    "def test_cleanup():\n    shared.reset()\n\n\n"
    "def test_victim():\n    assert a() == 1\n    assert shared.SEEN == []\n"
)
VICTIM = "tests/test_p.py::test_victim"


@pytest.fixture
def polluted(repo):
    """A suite where one test leaves state behind that breaks a later one,
    unless the test between them (which no change to a() selects) ran."""
    repo.write("pkg/shared.py", SHARED)
    repo.write("tests/test_p.py", POLLUTED)
    repo.commit("a polluter, its cleanup and a victim")
    p = repo.record()
    assert p.returncode == 0, p.stdout + p.stderr
    return repo


def test_a_failure_that_passes_without_the_other_selected_tests_is_settled_by_the_suite(polluted):
    polluted.edit("pkg/mod.py", "return 1", "return 0 + 1")
    out = polluted.cli("run")
    # the selected run failed test_victim, which passes alone: the whole
    # suite, where test_cleanup runs between the two, is what decides
    assert out["selection"]["mode"] == "run_all" and out["selection"]["run_all_reasons"] == [
        "1 failure(s) of the selected run passed without the other selected tests: "
        "the whole suite settles them"]
    assert out["failures"] == []
    (entry,) = out["order_dependent"]
    assert (entry["test"], entry["alone"], entry["suite"]) == (VICTIM, "passed", "passed")
    assert entry["error"] == "assert [1] == []"
    s = out["summary"]
    assert (s["status"], s["failed"], s["order_dependent"], s["ran"]) == ("passed", 0, 1, 6)
    assert out["selected_run"]["ran"] == 4
    # both outcomes are in history, on one tree: from now on the test is
    # flaky, and its retries settle it without the suite
    assert polluted.cli("affected")["flaky"][VICTIM]["failed"] == 1
    polluted.edit("pkg/mod.py", "return 0 + 1", "return 1 + 0")
    again = polluted.cli("run")
    assert again["selection"]["mode"] == "select" and again["summary"]["status"] == "passed"
    assert again["summary"]["flaky"]["passed_on_retry"] == 1


def test_what_selected_tests_leave_in_the_environment_stays_out_of_later_processes(polluted):
    # the selected tests run in the process that then starts the others (the
    # failures alone, the whole suite): a variable a test set and never unset
    # must not be what those start with, or the suite fails for no change
    polluted.write("tests/conftest.py", "import os\n\nSTARTED_WITH = os.environ.get('LEAKED')\n")
    polluted.write("tests/test_p.py", "import os\n\nimport conftest\n" + POLLUTED.replace(
        "    shared.SEEN.append(a())\n", "    shared.SEEN.append(a())\n    os.environ['LEAKED'] = '1'\n")
        + "\n\ndef test_started_clean():\n    assert conftest.STARTED_WITH is None\n")
    polluted.commit("a test that leaks a variable")
    assert polluted.record().returncode == 0
    polluted.edit("pkg/mod.py", "return 1", "return 0 + 1")
    out = polluted.cli("run")
    (entry,) = out["order_dependent"]
    assert (entry["test"], entry["alone"], entry["suite"]) == (VICTIM, "passed", "passed")
    s = out["summary"]
    assert (s["status"], s["failed"], s["ran"]) == ("passed", 0, 7)


def test_a_change_that_makes_one_test_break_another_fails(repo):
    # no test tidies up here: with the change, test_polluter leaves state
    # behind in the suite too. test_victim passes alone, and that is no excuse
    repo.write("pkg/shared.py", "SEEN = []\n\n\ndef note(value):\n    if value != 1:\n"
               "        SEEN.append(value)\n")
    repo.write("tests/test_p.py", "from pkg import shared\nfrom pkg.mod import a\n\n\n"
               "def test_polluter():\n    shared.note(a())\n\n\n"
               "def test_victim():\n    assert a() in (1, 5)\n    assert shared.SEEN == []\n")
    repo.commit("a test that pollutes once a() changes")
    assert repo.record().returncode == 0
    repo.edit("pkg/mod.py", "return 1", "return 5")
    out = repo.cli("run", code=1)
    assert out["selection"]["mode"] == "run_all"
    (entry,) = out["order_dependent"]
    assert (entry["test"], entry["alone"], entry["suite"]) == (VICTIM, "passed", "failed")
    failed = {f["representative"] for f in out["failures"]} | {
        t for f in out["failures"] for t in f["also_failed"]}
    assert VICTIM in failed and out["summary"]["status"] == "failed"


def test_an_isolated_pass_can_be_taken_at_its_word(polluted):
    polluted.edit("pkg/mod.py", "return 1", "return 0 + 1")
    out = polluted.cli("run", "--isolate-failures", "1")
    assert out["selection"]["mode"] == "select" and out["selection"]["n_selected"] == 4
    (entry,) = out["order_dependent"]
    assert (entry["test"], entry["alone"]) == (VICTIM, "passed") and "suite" not in entry
    s = out["summary"]
    assert (s["status"], s["failed"], s["order_dependent"]) == ("passed", 0, 1)


def test_a_failure_that_also_fails_alone_is_a_failure(polluted):
    polluted.edit("pkg/mod.py", "return 1", "return 5")
    out = polluted.cli("run", code=1)
    assert "order_dependent" not in out
    assert out["summary"]["status"] == "failed" and out["summary"]["order_dependent"] == 0
    failed = {f["representative"] for f in out["failures"]} | {
        t for f in out["failures"] for t in f["also_failed"]}
    assert VICTIM in failed


def test_failures_are_not_rerun_alone_when_asked_not_to(polluted):
    polluted.edit("pkg/mod.py", "return 1", "return 0 + 1")
    out = polluted.cli("run", "--isolate-failures", "0", code=1)
    assert out["summary"]["status"] == "failed" and "order_dependent" not in out


UNSTABLE = (
    "import os\n\nimport pytest\n\nfrom pkg.mod import a, b\n\n\n"
    "@pytest.mark.parametrize('stamp', ['fixed', str(os.getpid())])\n"
    "def test_stamp(stamp):\n    assert a() == 1\n\n\n"
    "@pytest.mark.parametrize('fn', [a, b], ids=[f'a-{os.getpid()}', 'b'])\n"
    "def test_fn(fn):\n    assert fn() in (1, 2)\n\n\n"
    "def test_other():\n    assert b() == 2\n"
)


@pytest.fixture
def unstable(repo):
    """Tests whose ids carry something that differs in every process."""
    repo.write("tests/test_u.py", UNSTABLE)
    repo.commit("parametrized by the process id")
    p = repo.record()
    assert p.returncode == 0, p.stdout + p.stderr
    return repo


def test_a_parametrized_test_is_targeted_as_its_function(unstable):
    unstable.edit("pkg/mod.py", "return 1", "return 0 + 1")
    sel = unstable.select()
    assert sum(t.startswith("tests/test_u.py::test_stamp[") for t in sel["selected"]) == 2
    assert sum(t.startswith("tests/test_u.py::test_fn[a-") for t in sel["selected"]) == 1
    # never `name[id]`: what the ids were when the map was recorded says
    # nothing about what they are in the next process
    assert sel["targets"] == [T_C, T_A, "tests/test_u.py::test_fn", "tests/test_u.py::test_stamp"]
    assert sel["deselect"] == ["tests/test_u.py::test_fn[b]"]


def test_ids_that_changed_since_the_map_run_under_their_new_names(unstable):
    unstable.edit("pkg/mod.py", "return 1", "return 0 + 1")
    out = unstable.cli("run")
    s = out["summary"]
    assert (s["status"], s["failed"], s["ran"], s["unrun_targets"]) == ("passed", 0, 5, [])
    c = out["conservation"]
    # test_stamp[<old pid>] and test_fn[a-<old pid>] ran as their new ids
    assert (c["conserved"], c["selected"], c["executed"], c["renamed"], c["unplanned"]) == (
        True, 5, 5, 2, 2)


def test_a_run_says_in_one_line_what_it_did(repo):
    """stdout is the JSON document; the line on stderr is for the log's reader."""
    def line() -> tuple[int, str]:
        p = repo._run("prongs", "run")
        return p.returncode, re.sub(r" in \d+s\n$", " in Ns", p.stderr)

    assert line() == (0, "prongs: passed: ran 3 tests (everything: no coverage map "
                         "(this run records one)) in Ns")
    assert line() == (0, "prongs: nothing_to_run: ran 0 of 3 tests "
                         "(3 not affected by the change) in Ns")
    repo.edit("pkg/mod.py", "return 2", "return 3")
    assert line() == (1, "prongs: failed: ran 2 of 3 tests (1 not affected by the change), "
                         "2 failed in Ns")
    repo.write("pkg/mod.py", MOD)
    assert line() == (0, "prongs: passed: ran 2 of 3 tests (1 not affected by the change) in Ns")
    # a test the change adds is not in the map's count, and is in this one
    repo.write("tests/test_new.py", "def test_new():\n    assert True\n")
    assert line() == (0, "prongs: passed: ran 3 of 4 tests (1 not affected by the change) in Ns")
