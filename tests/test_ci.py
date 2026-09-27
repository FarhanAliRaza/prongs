"""The CI artifact flow: the map and journal files travel between fresh
clones as artifact directories (`bolttest ci save` / `bolttest ci restore`),
the way CI jobs pass them through a cache or uploaded artifacts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from conftest import T_A, T_B, T_C, Repo


def clone(src: Repo, dest: Path, *args: str) -> Repo:
    """A CI job's checkout of `src` (over file://, so --depth is honoured)."""
    subprocess.run(["git", "clone", "-q", *args, src.path.as_uri(), str(dest)],
                   check=True, capture_output=True)
    job = Repo(dest)
    job.git("config", "user.email", "ci@example.com")
    job.git("config", "user.name", "ci")
    return job


def docs(repo: Repo, n: int) -> None:
    for i in range(n):
        repo.write("README.md", f"revision {i}\n")
        repo.commit(f"docs {i}")


def test_a_fresh_clone_selects_from_a_restored_map(recorded, tmp_path):
    art = tmp_path / "artifact"
    saved = recorded.cli("ci", "save", str(art))
    assert saved["map"]["tests"] == 3 and saved["map"]["rollup_commit"] == recorded.head()
    manifest = json.loads((art / "manifest.json").read_text())
    assert manifest["head"] == recorded.head() and len(manifest["journals"]) == 1
    job = clone(recorded, tmp_path / "job")
    res = job.cli("ci", "restore", str(tmp_path))  # finds the artifact below the path
    assert res["map"]["installed"] is True and res["history"]["missing"] == 0
    assert res["journals"]["known"] == 1  # the map already holds that run
    job.edit("pkg/mod.py", "return 2", "return 2  # edited")
    out = job.cli("affected")
    assert out["mode"] == "select" and out["tests"] == sorted([T_B, T_C])


def test_restore_deepens_a_shallow_clone_to_the_maps_commit(recorded, tmp_path):
    mapped = recorded.head()
    docs(recorded, 5)
    art = tmp_path / "artifact"
    recorded.cli("ci", "save", str(art))
    job = clone(recorded, tmp_path / "job", "--depth", "1")
    assert job.git("rev-parse", "--is-shallow-repository") == "true"
    # without the history, the map's age is unknown: everything runs, and the
    # receipt says why
    res = job.cli("ci", "restore", "--no-fetch", str(art))
    assert res["history"]["still_missing"] == [mapped] and len(res["warnings"]) == 1
    out = job.cli("affected")
    assert out["mode"] == "run_all" and "shallow clone" in out["run_all_reasons"][0]
    res = job.cli("ci", "restore", str(art))
    assert res["history"]["still_missing"] == [] and res["history"]["fetches"][0]["ok"]
    out = job.cli("affected")
    assert out["mode"] == "select" and out["tests"] == []
    assert out["skip_receipt"]["map_age"]["commits_behind_head"] == 5


def test_a_pull_requests_merge_checkout_is_deepened_to_the_map(recorded, tmp_path):
    art = tmp_path / "artifact"
    recorded.cli("ci", "save", str(art))
    docs(recorded, 2)  # main moves on after the map was saved
    main = recorded.git("rev-parse", "--abbrev-ref", "HEAD")
    recorded.git("checkout", "-q", "-b", "feature")
    recorded.edit("pkg/mod.py", "return 2", "return 2  # pr")
    recorded.commit("pr edit")
    recorded.git("checkout", "-q", main)
    recorded.git("merge", "-q", "--no-ff", "--no-edit", "feature")
    # the forge's test merge commit lives on no branch, and the pull
    # request's branch (a fork's) is not in the repository either
    recorded.git("update-ref", "refs/pull/1/merge", recorded.head())
    recorded.git("reset", "-q", "--hard", "HEAD~1")
    recorded.git("branch", "-qD", "feature")
    # what actions/checkout does for a pull request: that one commit, depth 1
    (tmp_path / "job").mkdir()
    job = Repo(tmp_path / "job")
    job.git("init", "-q")
    job.git("remote", "add", "origin", recorded.path.as_uri())
    job.git("fetch", "-q", "--no-tags", "--depth=1", "origin",
            "+refs/pull/1/merge:refs/remotes/pull/1/merge")
    job.git("checkout", "-q", "--detach", "refs/remotes/pull/1/merge")
    res = job.cli("ci", "restore", str(art))
    assert res["history"]["still_missing"] == [] and res["warnings"] == []
    out = job.cli("affected")
    assert out["mode"] == "select" and out["tests"] == sorted([T_B, T_C])
    # two docs commits, the pull request's commit and the merge commit
    assert out["skip_receipt"]["map_age"]["commits_behind_head"] == 4


def test_a_shallow_clone_never_skips_tests_observed_on_a_missing_commit(recorded, tmp_path):
    # the map's full run is old; a newer partial run is its latest commit.
    # A shallow clone has the latter, so the map's age looks fine, but the
    # changes since the full run are invisible to it
    recorded.edit("pkg/mod.py", "return 2", "return 3")  # breaks test_b and test_c
    recorded.commit("break b")
    docs(recorded, 2)
    assert recorded.record(T_A).returncode == 0
    art = tmp_path / "artifact"
    recorded.cli("ci", "save", str(art))
    job = clone(recorded, tmp_path / "job", "--depth", "1")
    res = job.cli("ci", "restore", "--no-fetch", str(art))
    assert res["warnings"] and "the tests observed there will run" in res["warnings"][0]
    out = job.cli("affected")
    assert out["mode"] == "select" and out["tests"] == sorted([T_B, T_C])
    (reason,) = out["selected_by_reason"]
    assert reason.startswith("evidence commit ") and reason.endswith(" is not in this repository")
    # with the history fetched, the same two tests are selected by the diff
    job.cli("ci", "restore", str(art))
    out = job.cli("affected")
    assert out["tests"] == sorted([T_B, T_C])
    assert out["selected_by_reason"] == {"touches changed function pkg/mod.py": 2}


def test_another_branchs_journal_is_folded_as_history_only(recorded, tmp_path):
    main_art = tmp_path / "main-artifact"
    recorded.cli("ci", "save", str(main_art))
    # a pull request's job: its own branch, a selected run that fails
    pr = clone(recorded, tmp_path / "pr")
    pr.git("checkout", "-q", "-b", "feature")
    pr.cli("ci", "restore", str(main_art))
    pr.edit("pkg/mod.py", "return 2", "return 3")
    pr.commit("break b on a branch")
    run = pr.cli("run", code=1)
    pr_art = tmp_path / "pr-artifact"
    saved = pr.cli("ci", "save", "--no-map", str(pr_art))
    assert saved["map"] is None and saved["journals"] == [run["summary"]["recorded"]["journal"]]
    # the next main job folds it: that branch's failures are not this branch's
    main = clone(recorded, tmp_path / "main")
    res = main.cli("ci", "restore", str(main_art), str(pr_art))
    assert res["journals"]["foreign"] == saved["journals"] and res["journals"]["own"] == []
    assert res["warnings"] == []  # the branch's commit is not evidence
    out = main.cli("affected")
    assert out["evidence"]["foreign_runs"] == 1 and out["history"]["recent_changes"] == 0
    assert out["mode"] == "select" and out["tests"] == []


def test_this_branchs_journals_are_own_evidence_and_save_carries_only_new_ones(
        recorded, tmp_path):
    art1 = tmp_path / "job1"
    recorded.cli("ci", "save", str(art1))
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    recorded.commit("edit b")
    # job 2 runs what the edit affects and saves its journal
    job2 = clone(recorded, tmp_path / "clone2")
    job2.cli("ci", "restore", str(art1))
    run = job2.cli("run")
    assert run["summary"]["ran"] == 2
    art2 = tmp_path / "job2"
    job2.cli("ci", "save", "--no-map", str(art2))
    # job 3, on the same commit, folds job 2's run as its own evidence
    job3 = clone(recorded, tmp_path / "clone3")
    res = job3.cli("ci", "restore", str(art1), str(art2))
    assert res["journals"]["own"] == [run["summary"]["recorded"]["journal"]]
    out = job3.cli("affected")
    assert out["mode"] == "select" and out["tests"] == [] and len(out["evidence"]["contributing_runs"]) == 2
    # restored files are not this job's: save carries only what job 3 wrote
    job3.edit("pkg/mod.py", "return 1", "return 1  # edited")
    mine = job3.cli("run")["summary"]["recorded"]["journal"]
    assert job3.cli("ci", "save", str(tmp_path / "job3"))["journals"] == [mine]


def test_restore_keeps_a_local_map_that_knows_more(recorded, tmp_path):
    art = tmp_path / "artifact"
    recorded.cli("ci", "save", str(art))
    recorded.edit("pkg/mod.py", "return 2", "return 2  # edited")
    recorded.cli("run")
    recorded.rollup()
    res = recorded.cli("ci", "restore", str(art))
    assert res["map"]["installed"] is False and "kept it" in res["map"]["reason"]
    assert len(recorded.runs()) == 2


def test_restore_with_no_artifact_is_a_first_run(repo, tmp_path):
    res = repo.cli("ci", "restore", str(tmp_path / "cache-miss"))
    assert res["artifacts"] == [] and res["map"] is None
    out = repo.cli("run")  # no map: everything runs, recorded
    assert out["selection"]["mode"] == "run_all" and out["summary"]["ran"] == 3
    saved = repo.cli("ci", "save", str(tmp_path / "artifact"))
    assert saved["map"]["tests"] == 3 and saved["map"]["runs"] == 1


def test_save_replaces_the_artifact_it_is_given(recorded, tmp_path):
    # a cache step saves the map into the directory it restored from: what
    # the new save does not write must go, or it rides along forever
    art = tmp_path / "artifact"
    recorded.cli("ci", "save", str(art))
    assert sorted(p.name for p in (art / "journal").iterdir()) != []
    recorded.cli("ci", "save", "--no-journals", str(art))
    assert sorted(p.name for p in art.iterdir()) == ["manifest.json", "map.sqlite"]
    # and it never clears a directory that is not an artifact
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "notes.txt").write_text("keep")
    out = recorded.cli("ci", "save", str(tmp_path / "mine"), code=2)
    assert "holds no artifact" in out["error"]
    assert (tmp_path / "mine" / "notes.txt").read_text() == "keep"


def test_a_foreign_journal_never_makes_restore_fetch_history(recorded, tmp_path):
    # a pull request's journal names a commit (its test merge commit) that a
    # clone of the main branch never has: only the map's evidence commits
    # are worth fetching, and a shallow main-branch clone must not be
    # unshallowed chasing the other
    main_art = tmp_path / "main-artifact"
    recorded.cli("ci", "save", str(main_art))
    pr = clone(recorded, tmp_path / "pr")
    pr.cli("ci", "restore", str(main_art))
    pr.edit("pkg/mod.py", "return 2", "return 2  # pr")
    pr.commit("a pull request's commit, pushed nowhere")
    pr.cli("run")
    pr_art = tmp_path / "pr-artifact"
    pr.cli("ci", "save", "--no-map", str(pr_art))
    docs(recorded, 2)
    main = clone(recorded, tmp_path / "main", "--depth", "1")
    res = main.cli("ci", "restore", str(main_art), str(pr_art))
    assert len(res["journals"]["foreign"]) == 1
    assert [f["args"][0] for f in res["history"]["fetches"]] == ["--deepen=64"]
    assert res["history"]["still_missing"] == [] and res["warnings"] == []
