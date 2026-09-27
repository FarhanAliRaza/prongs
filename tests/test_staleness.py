"""The map-staleness safety net: receipts carry the map's age and unmapped
count; code the map has never seen broadens selection instead of silently
selecting nothing; a map too far behind HEAD runs everything."""

from __future__ import annotations

from conftest import T_A, T_B, T_C


def advance(repo, n: int) -> None:
    """n commits that change nothing a test can see."""
    for i in range(n):
        repo.write("README.md", f"revision {i}\n")
        repo.commit(f"docs {i}")


def test_receipt_reports_map_age_and_unmapped_tests(recorded):
    receipt = recorded.cli("affected")["skip_receipt"]
    assert receipt["map_age"] == {
        "commit": recorded.head()[:8], "commits_behind_head": 0, "max_map_age": 50,
    }
    assert receipt["unmapped_tests"] == 0 and receipt["broadened"] == []
    advance(recorded, 2)
    receipt = recorded.cli("affected")["skip_receipt"]
    assert receipt["map_age"]["commits_behind_head"] == 2 and receipt["skipped"] == 3


def test_a_changed_function_the_map_never_saw_selects_its_whole_file(recorded):
    recorded.edit("pkg/mod.py", "def c():", "def d():\n    return 4\n\n\ndef c():")
    sel = recorded.select()
    assert sel["mode"] == "select" and set(sel["selected"]) == {T_A, T_B, T_C}
    reason = "changed function not in the map: pkg/mod.py::d; every test that touches pkg/mod.py runs"
    assert sel["broadened"] == [reason] and sel["selected"][T_A] == reason


def test_a_function_added_after_the_map_was_built_is_not_a_silent_miss(recorded):
    # b() starts calling a new d(), and a later commit changes d(). Diffing
    # only the last commit, as a CI job against its parent does, shows just
    # d(): the map never saw it, and without the safety net nothing ran.
    recorded.edit("pkg/mod.py", "def b():\n    return 2", "def d():\n    return 1\n\n\ndef b():\n    return d() + 1")
    parent = recorded.commit("b() delegates to a new d()")
    recorded.edit("pkg/mod.py", "    return 1\n\n\ndef b", "    return 5\n\n\ndef b")
    recorded.commit("change d()")
    sel = recorded.select(base=parent)
    assert sel["changed_functions"] == ["pkg/mod.py::d"]
    assert T_B in sel["selected"] and sel["broadened"][0].startswith(
        "changed function not in the map: pkg/mod.py::d"
    )


def test_a_new_file_the_map_never_saw_runs_everything(recorded):
    # a new module can be imported by discovery alone (Django's admin.py,
    # wagtail_hooks.py) with no other change to show for it
    recorded.write("pkg/admin.py", "REGISTERED = True\n")
    sel = recorded.select()
    assert sel["mode"] == "run_all"
    assert sel["reasons"] == [
        "changed file not in the map: pkg/admin.py (it did not exist when the map's "
        "evidence was recorded)"
    ]


def test_a_file_that_existed_unimported_when_the_map_was_recorded_stays_precise(repo):
    repo.write("scripts/tool.py", "def main():\n    return 0\n")
    repo.commit("a script no test imports")
    assert repo.record().returncode == 0
    repo.edit("scripts/tool.py", "return 0", "return 1")
    sel = repo.select()
    assert sel["mode"] == "select" and sel["targets"] == [] and sel["broadened"] == []


def test_a_map_too_far_behind_head_runs_everything(recorded):
    advance(recorded, 3)
    out = recorded.cli("affected", "--max-map-age", "2")
    assert out["mode"] == "run_all"
    assert out["run_all_reasons"] == ["map is 3 commits behind HEAD (max_map_age 2)"]
    assert out["skip_receipt"]["map_age"]["commits_behind_head"] == 3
    recorded.env["BOLTTEST_MAX_MAP_AGE"] = "3"
    assert recorded.cli("affected")["mode"] == "select"


def test_evidence_from_a_commit_this_clone_lacks_is_never_diffed_from_head(recorded):
    # test_b's newest evidence comes from a commit that is gone (a deleted
    # branch; in CI, a shallow clone), while the map's own commit is here
    main = recorded.git("rev-parse", "--abbrev-ref", "HEAD")
    recorded.git("checkout", "-q", "-b", "side")
    recorded.edit("pkg/mod.py", "return 2", "return 2  # side")
    side = recorded.commit("side edit")
    assert recorded.record("tests/test_m.py::test_b").returncode == 0
    recorded.git("checkout", "-q", main)
    assert recorded.record("tests/test_m.py::test_a").returncode == 0
    recorded.git("branch", "-qD", "side")
    recorded.git("reflog", "expire", "--expire=now", "--all")
    recorded.git("gc", "-q", "--prune=now")
    recorded.edit("pkg/mod.py", "return 2", "return 3")  # breaks test_b and test_c
    recorded.commit("break b")
    sel = recorded.select()
    assert sel["mode"] == "select" and sel["evidence"]["map_age"]["commits_behind_head"] == 1
    # diffing test_b's evidence from HEAD would see no change and skip it
    assert set(sel["selected"]) == {T_B, T_C}
    assert sel["selected"][T_B] == (
        f"evidence commit {side[:8]} is not in this repository: nothing to diff it against"
    )


def test_a_map_of_unknown_age_runs_everything(recorded):
    recorded.rollup()
    con = recorded.db()
    con.execute("UPDATE meta SET value='deadbeef' || substr(value, 9) WHERE key='rollup_commit'")
    con.commit()
    sel = recorded.select()
    assert sel["mode"] == "run_all"
    assert sel["reasons"] == ["map age unknown: map commit deadbeef is not in this repository"]
