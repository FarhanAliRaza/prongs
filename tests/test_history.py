"""History as the second signal: a test whose outcome flipped in the last N
rollups is selected; a test that passed and failed on one tree is flaky —
still run, reported apart from failures, with its history as the receipt."""

from __future__ import annotations

import os

import pytest

from prongs import journal, mapdb
from prongs.select import flaky_tests, recent_status_changes
from prongs.select import test_history as history_of


@pytest.fixture
def root(tmp_path):
    return str(tmp_path) + os.sep


def rolled(tmp_path, root, statuses: dict, commit="c0", dirty=None, **run):
    """Append one run's outcomes and roll it up (one rollup per run)."""
    jdir = tmp_path / "journal"
    records = {t: (0.1, s, [(root + "pkg/mod.py", "f", 1)]) for t, s in statuses.items()}
    journal.append(jdir, root, records, {"scope": "partial", "commit": commit,
                                         "dirty_files": dirty or {}, "recorder": "r1", **run})
    mapdb.rollup(tmp_path / "map.sqlite", jdir)
    return mapdb.connect(str(tmp_path / "map.sqlite"))


def test_a_flip_selects_the_test_for_the_next_n_rollups(tmp_path, root):
    rolled(tmp_path, root, {"t::a": "passed", "t::b": "passed"})
    con = rolled(tmp_path, root, {"t::a": "failed"})  # rollup 2: a flips
    assert recent_status_changes(con, 1) == {"t::a": "recent status change: passed -> failed in run #2"}
    con = rolled(tmp_path, root, {"t::b": "passed"})  # rollup 3: no flip
    assert recent_status_changes(con, 1) == {}
    assert set(recent_status_changes(con, 2)) == {"t::a"}
    con = rolled(tmp_path, root, {"t::a": "skipped"})  # not an outcome: no flip
    assert set(recent_status_changes(con, 1)) == set()
    con = rolled(tmp_path, root, {"t::a": "passed"})  # back: compared with 'failed'
    assert recent_status_changes(con, 1) == {"t::a": "recent status change: failed -> passed in run #5"}
    assert recent_status_changes(con, 0) == {}


def test_flaky_means_both_outcomes_on_one_tree(tmp_path, root):
    rolled(tmp_path, root, {"t::same": "passed", "t::edited": "passed", "t::moved": "passed"})
    rolled(tmp_path, root, {"t::same": "failed"})
    # a different dirty content is a different tree: a fix, not a flake
    rolled(tmp_path, root, {"t::edited": "failed"}, dirty={"pkg/mod.py": ["h1", "h1"]})
    # a tree that changed under the run proves nothing
    con = rolled(tmp_path, root, {"t::moved": "failed"}, tree_changed=True)
    assert flaky_tests(con) == {"t::same": {"tree": "c0", "passed": 1, "failed": 1, "last_run": 2}}


def test_a_polluted_test_is_selected_on_the_run_after_its_failure(recorded):
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
    polluted = "tests/test_p.py::test_2_reads_state"
    recorded.edit("pkg/mod.py", "return 1", "return 5")
    assert polluted not in recorded.cli("affected")["tests"]  # coverage cannot see it
    assert recorded.record().returncode == 1  # the failure: a full run, recorded
    out = recorded.cli("affected")
    assert out["history"] == {"window": 3, "recent_changes": 4, "flaky": 0, "flaky_window": 50}
    assert polluted in out["tests"]
    # the failing run observed this very tree, so coverage alone would now
    # select nothing: every test that just flipped comes back through history
    assert out["selected_by_reason"] == {"recent status change": 4}
    # the fix: the polluted test is re-verified along with the tests of a()
    recorded.edit("pkg/mod.py", "return 5", "return 1")
    run = recorded.cli("run")
    assert run["summary"]["status"] == "passed" and run["conservation"]["conserved"]
    path = journal.find(journal.journal_dir(recorded.path), run["summary"]["recorded"]["journal"])
    assert journal.statuses(path)[polluted] == "passed"


COIN = (
    "import os, pathlib\n\n"
    "def test_coin():\n"
    "    assert not os.environ.get('COIN_BROKEN')\n"
    "    p = pathlib.Path(os.environ['COIN'])\n"
    "    n = int(p.read_text()) if p.exists() else 0\n"
    "    p.write_text(str(n + 1))\n"
    "    assert n % 2 == 0\n"
)
COIN_ID = "tests/test_coin.py::test_coin"


@pytest.fixture
def coin(recorded, tmp_path):
    """A test that passes on even calls and fails on odd ones, recorded
    passing once (call 0)."""
    recorded.env["COIN"] = str(tmp_path / "coin")  # outside the repo: the tree never changes
    recorded.write("tests/test_coin.py", COIN)
    recorded.commit("a coin-flip test")
    assert recorded.record().returncode == 0
    return recorded


def test_a_flake_passes_on_retry_and_is_reported_apart_from_failures(coin):
    # run by selection on the same tree, it fails (call 1): that contradicts
    # the pass, so it is retried alone, passes (call 2), and is a flake
    out = coin.cli("run", "--base", "HEAD~1")  # selects the new test file
    assert out["summary"]["status"] == "passed" and out["failures"] == []
    (f,) = out["flaky"]
    assert (f["test"], f["status"], f["retries"], f["counted_as"]) == (
        COIN_ID, "failed", ["passed"], "flaky")
    assert f["why"].startswith("passed 1x and failed 1x on tree ")
    assert [h["status"] for h in f["history"]] == ["passed"]
    assert out["summary"]["flaky"] == {"ran": 1, "failed": 1, "passed_on_retry": 1, "retries": 2}
    # rolled up — the retry's pass is recorded next to the failure — it is
    # known flaky: still selected (its outcome flipped), retried again
    out = coin.cli("affected")
    assert COIN_ID in out["tests"] and out["flaky"][COIN_ID]["failed"] == 1
    out = coin.cli("run")  # call 3 fails, call 4 passes
    assert out["summary"]["status"] == "passed" and out["flaky"][0]["counted_as"] == "flaky"


def test_a_known_flake_that_fails_every_retry_is_a_failure(coin):
    coin.cli("run", "--base", "HEAD~1")  # the flake, known from now on
    coin.env["COIN_BROKEN"] = "1"  # now it fails for real, on the same tree
    out = coin.cli("run", code=1)
    assert out["summary"]["status"] == "failed" and out["summary"]["failed"] == 1
    (f,) = out["flaky"]
    assert f["retries"] == ["failed", "failed"] and f["counted_as"] == "failure"
    assert out["failures"][0]["representative"] == COIN_ID


def test_no_retries_makes_a_flaky_failure_a_failure(coin):
    out = coin.cli("run", "--base", "HEAD~1", "--flaky-retries", "0", code=1)
    (f,) = out["flaky"]
    assert f["retries"] == [] and f["counted_as"] == "failure"
    assert out["summary"]["failed"] == 1


def test_flaky_evidence_expires(tmp_path, root):
    rolled(tmp_path, root, {"t::a": "passed"})
    con = rolled(tmp_path, root, {"t::a": "failed"})  # same tree: a flake
    assert set(flaky_tests(con, 2)) == {"t::a"}
    con = rolled(tmp_path, root, {"t::b": "passed"})  # a rollup without it
    assert set(flaky_tests(con, 3)) == {"t::a"}
    assert flaky_tests(con, 2) == {}  # the pass has left the window
    assert flaky_tests(con, 0) == {}


def test_foreign_outcomes_never_count_as_a_recent_flip_but_do_show_flakes(tmp_path, root):
    rolled(tmp_path, root, {"t::a": "passed", "t::b": "passed"}, commit="main1")
    jdir = tmp_path / "journal"
    foreign = jdir / journal.FOREIGN
    records = {"t::a": (0.1, "failed", [(root + "pkg/mod.py", "f", 1)])}
    # a pull request broke t::a on its own branch: not this branch's news
    journal.append(foreign, root, records, {"scope": "partial", "commit": "pr1", "recorder": "r1"})
    # and t::b flaked on another branch's tree: passed, then failed, same tree
    for status in ("passed", "failed"):
        journal.append(foreign, root, {"t::b": (0.1, status, [(root + "pkg/mod.py", "f", 1)])},
                       {"scope": "partial", "commit": "pr2", "recorder": "r1"})
    mapdb.rollup(tmp_path / "map.sqlite", jdir)
    con = mapdb.connect(str(tmp_path / "map.sqlite"))
    assert recent_status_changes(con, 3) == {}
    assert flaky_tests(con) == {"t::b": {"tree": "pr2", "passed": 1, "failed": 1, "last_run": 4}}
    assert [h.get("foreign", False) for h in history_of(con, "t::b")] == [False, True, True]
