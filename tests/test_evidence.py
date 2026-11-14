"""evidence.py: what a test depends on beyond the functions it enters
(shared fixtures, files it reads, names it lists, processes it starts), and
the shadow audit of runs that execute everything."""

from __future__ import annotations

import json

import pytest

from prongs import journal

MOD = '''import json
import subprocess
import sys
from pathlib import Path


def build():
    return {"v": 1}


def child_only():
    return 7


def spawn():
    code = "from pkg.mod import child_only; print(child_only())"
    return int(subprocess.check_output([sys.executable, "-c", code]))


def git_version():
    return subprocess.check_output(["git", "--version"]).decode()


def table():
    return json.loads(Path("data/table.json").read_text())


def unrelated():
    return 0
'''
TESTS = '''import pytest
from pkg import mod


@pytest.fixture(scope="module")
def built():
    return mod.build()


def test_one(built):
    assert built["v"] == 1


def test_two(built):
    assert built["v"] == 1


def test_spawn():
    assert mod.spawn() == 7


def test_git():
    assert mod.git_version().startswith("git")


def test_table():
    assert mod.table() == {"k": 1}


def test_names():
    assert [n for n in dir(mod) if n.startswith("child")] == ["child_only"]


def test_workflow():
    assert "jobs" in open(".github/workflows/ci.yml").read()


def test_plain():
    assert mod.unrelated() == 0
'''
T = "tests/test_e.py::"


@pytest.fixture
def project(repo):
    repo.write("pkg/mod.py", MOD)
    repo.write("tests/test_e.py", TESTS)
    (repo.path / "tests/test_m.py").unlink()
    repo.write("data/table.json", '{"k": 1}\n')
    repo.write("data/unused.json", "{}\n")
    repo.write(".github/workflows/ci.yml", "jobs: {}\n")
    repo.write(".github/workflows/other.yml", "jobs: {}\n")
    repo.commit("project")
    # whether prongs.pth is installed depends on how prongs was: off, unless a test asks
    repo.env["PRONGS_RECORD_CHILDREN"] = "0"
    p = repo.record()
    assert p.returncode == 0, p.stdout + p.stderr
    return repo


def selected(repo) -> dict[str, str]:
    sel = repo.select()
    assert sel["mode"] == "select", sel.get("reasons")
    return {t.removeprefix(T): why for t, why in sel["selected"].items()}


def test_a_shared_fixture_is_credited_to_every_test_that_uses_it(project):
    project.edit("pkg/mod.py", 'return {"v": 1}', 'return {"v": 2}')
    assert selected(project) == {
        "test_one": "touches changed function pkg/mod.py::build",
        "test_two": "touches changed function pkg/mod.py::build",
        "test_spawn": "starts a process prongs could not record",
        "test_git": "starts a process prongs could not record",
    }
    assert project.cli("run", code=1)["summary"]["failed"] == 2


def test_a_test_that_starts_an_unrecorded_process_runs_on_any_code_change(project):
    # child_only runs in the child alone: no test enters it, and the test
    # that depends on it is found by its process marker
    project.edit("pkg/mod.py", "return 7", "return 8")
    sel = project.select()
    assert {t.removeprefix(T): w for t, w in sel["selected"].items()} == {
        "test_spawn": "starts a process prongs could not record",
        "test_git": "starts a process prongs could not record",
    }
    assert sel["units"]["unexecuted"] == ["pkg/mod.py::child_only"]
    assert sel["changed_functions"] == ["pkg/mod.py::child_only"]
    # formatting changes no code: no process can tell
    project.git("checkout", "--", "pkg/mod.py")
    project.edit("pkg/mod.py", "return 7", "return 7  # seven")
    assert selected(project) == {}


def test_a_recorded_child_process_is_credited_to_its_test(project, tmp_path):
    # what prongs.pth does in an installed environment
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "sitecustomize.py").write_text(
        "import os\nif os.environ.get('PRONGS_CHILD'):\n    import prongs.child\n")
    project.env["PYTHONPATH"] = str(tmp_path / "site")
    project.env["PRONGS_RECORD_CHILDREN"] = "1"
    project.write("pyproject.toml", project.read("pyproject.toml")
                  + '\n[tool.prongs]\nnative_commands = ["git"]\ninert = ["pyproject.toml"]\n')
    project.commit("git is a native command")
    assert project.record().returncode == 0
    project.rollup()
    con = project.db()
    deps = {(p, q) for p, q in con.execute(
        "SELECT f.path, fn.qualname FROM tests t JOIN current_links l ON l.test_id = t.id "
        "JOIN funcs fn ON fn.id = l.func_id JOIN files f ON f.id = fn.file_id "
        "WHERE t.test_id = ?", (T + "test_spawn",))}
    con.close()
    assert deps == {("pkg/__init__.py", "<module>"), ("pkg/mod.py", "<module>"),
                    ("pkg/mod.py", "child_only"), ("pkg/mod.py", "spawn"),
                    ("tests/test_e.py", "test_spawn")}
    project.edit("pkg/mod.py", "return 7", "return 8")
    assert selected(project) == {
        "test_spawn": "touches changed function pkg/mod.py::child_only"}
    # a native program runs no project code: an unrelated change selects nobody
    project.git("checkout", "--", "pkg/mod.py")
    project.edit("pkg/mod.py", "return 0", "return 0 + 0")
    assert selected(project) == {"test_plain": "touches changed function pkg/mod.py::unrelated"}
    # but it may read a changed file
    project.edit("data/table.json", '{"k": 1}', '{"k": 1, "j": 2}')
    assert selected(project) == {
        "test_plain": "touches changed function pkg/mod.py::unrelated",
        "test_table": "reads changed file data/table.json",
        "test_git": "starts a native program that may read the changed file",
    }


def test_a_changed_data_file_selects_the_tests_that_read_it(project):
    project.edit("data/table.json", '{"k": 1}', '{"k": 2}')
    assert selected(project) == {
        "test_table": "reads changed file data/table.json",
        "test_spawn": "starts a process prongs could not record",
        "test_git": "starts a process prongs could not record",
    }
    # a file no test was seen reading has nothing to vouch for it
    project.git("checkout", "--", "data/table.json")
    project.edit("data/unused.json", "{}", '{"a": 1}')
    sel = project.select()
    assert sel["mode"] == "run_all"
    assert sel["reasons"] == ["non-Python file changed: data/unused.json"]


def test_a_file_a_test_reads_is_never_inert_by_default(project):
    project.edit(".github/workflows/other.yml", "jobs: {}", "jobs: {a: 1}")
    assert selected(project) == {}  # under .github/, and nothing reads it
    project.edit(".github/workflows/ci.yml", "jobs: {}", "name: ci")
    sel = selected(project)
    assert sel["test_workflow"] == "reads changed file .github/workflows/ci.yml"
    # the user's own inert patterns are a decision, not a default
    project.write("pyproject.toml", project.read("pyproject.toml")
                  + '\n[tool.prongs]\ninert = ["*.yml", "pyproject.toml"]\n')
    assert selected(project) == {}


def test_a_test_that_lists_a_modules_names_sees_a_new_one(project):
    project.edit("pkg/mod.py", "def unrelated():", "def child_extra():\n    return 1\n\n\ndef unrelated():")
    sel = project.select()
    assert {t.removeprefix(T): w for t, w in sel["selected"].items()} == {
        "test_names": "lists the names of pkg/mod.py"}
    assert sel["units"]["additive"] == {"pkg/mod.py": ["new def child_extra (line 28)"]}
    assert project.cli("run", "--no-record", code=1)["summary"]["failed"] == 1
    # a changed body adds no name
    project.git("checkout", "--", "pkg/mod.py")
    project.edit("pkg/mod.py", "return 0", "return 0 + 0")
    assert "test_names" not in selected(project)


def test_a_run_of_everything_audits_the_rules_it_overrode(project):
    # a module-level change in an import-time file runs everything; the
    # shadow says what selecting by the remaining rules would have done
    project.edit("pkg/mod.py", "def child_only():", "STATE = build()\n\n\ndef child_only():")
    project.edit("pkg/mod.py", "return 0", "return 1")
    out = project.cli("run", code=1)
    assert out["selection"]["mode"] == "run_all"
    assert out["shadow"] == {
        "reasons": ["module-level change in import-time file: pkg/mod.py"],
        "status_changes": 1,
        "rules": {"selection": {"would_select": 7, "would_skip": 1, "missed": [], "n_missed": 0}},
    }
    log = (journal.state_dir(project.path) / "shadow.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["rules"]["selection"]["n_missed"] == 0
    assert project.cli("shadow") == {"runs": 1, "clean": 1, "by_rule": {"selection": {
        "module-level change in import-time file": {
            "runs": 1, "would_skip": 1, "status_changes": 1, "n_missed": 0, "missed": []}}}}



def test_import_state_is_audited_in_shadow_when_asked_for(project):
    project.write("pkg/tables.py", "from pkg.mod import build\n\nTABLE = {'k': 1}\n"
                  "OTHER = build()\n\n\ndef lookup():\n    return TABLE['k']\n\n\n"
                  "def other():\n    return OTHER['v']\n")
    project.write("tests/test_t.py", "from pkg import tables\n\n\ndef test_lookup():\n"
                  "    assert tables.lookup() == 1\n\n\ndef test_other():\n"
                  "    assert tables.other() == 1\n")
    project.write("pyproject.toml", project.read("pyproject.toml")
                  + "\n[tool.prongs]\nimport_state = true\n")
    project.commit("tables, and the import-state shadow")
    assert project.record().returncode == 0
    project.rollup()
    con = project.db()
    key = con.execute("SELECT journal_key FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.close()
    snap = json.loads((journal.state_dir(project.path) / "state" / f"{key}.json").read_text())
    assert sorted(snap["modules"]["pkg/tables.py"]) == ["OTHER", "TABLE", "build", "lookup", "other"]
    assert snap["volatile"] == []
    # module-level data changes: everything runs; import-state would select
    # the test that reaches TABLE, and the one whose outcome changed is in it
    project.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    out = project.cli("run", "--no-record", code=1)
    assert out["selection"]["mode"] == "run_all"
    assert out["import_state"] == {"globals": 1, "classes": 0, "names": ["TABLE"], "volatile": 0}
    rules = out["shadow"]["rules"]
    # (the two tests that start a process run on any code change)
    assert rules["import_state"] == {
        "would_select": 4, "would_skip": 6, "missed": [], "n_missed": 0}
    assert rules["selection"]["would_select"] == 4
    # an import-time function changes what a global holds: the tests that
    # reach the global are found though they never enter the function
    project.git("checkout", "--", "pkg/tables.py")
    project.edit("pkg/mod.py", 'return {"v": 1}', 'return {"v": 3}')
    out = project.cli("run", "--no-record", code=1)
    assert out["selection"]["run_all_reasons"] == [
        "executed at import/collection time: pkg/mod.py::build"]
    assert out["import_state"]["names"] == ["OTHER"]
    assert out["shadow"]["status_changes"] == 3
    assert out["shadow"]["rules"]["import_state"]["n_missed"] == 0
    assert out["shadow"]["rules"]["selection"]["missed"] == ["tests/test_t.py::test_other"]
