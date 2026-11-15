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


def test_a_process_started_through_posix_spawn_is_one_process(project, tmp_path):
    # subprocess starts a child with os.posix_spawn where it can (always
    # without close_fds; on a recent glibc, from Python 3.13, by default), and
    # the interpreter then reports both calls: that is one process, and its
    # one report accounts for it
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "sitecustomize.py").write_text(
        "import os\nif os.environ.get('PRONGS_CHILD'):\n    import prongs.child\n")
    project.env["PYTHONPATH"] = str(tmp_path / "site")
    project.env["PRONGS_RECORD_CHILDREN"] = "1"
    project.edit("pkg/mod.py", '[sys.executable, "-c", code]))',
                 '[sys.executable, "-c", code], close_fds=False))')
    project.commit("posix_spawn")
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



@pytest.fixture
def tables(project):
    """The project plus a module whose globals import-time code builds, with
    the import-state rule on and a snapshot recorded."""
    project.write("pkg/tables.py", "from pkg.mod import build\n\nTABLE = {'k': 1}\n"
                  "OTHER = build()\n\n\ndef lookup():\n    return TABLE['k']\n\n\n"
                  "def other():\n    return OTHER['v']\n")
    project.write("tests/test_t.py", "from pkg import tables\n\n\ndef test_lookup():\n"
                  "    assert tables.lookup() == 1\n\n\ndef test_other():\n"
                  "    assert tables.other() == 1\n")
    project.write("pyproject.toml", project.read("pyproject.toml")
                  + "\n[tool.prongs]\nimport_state = true\n")
    project.commit("tables, and the import-state rule")
    assert project.record().returncode == 0
    return project


def test_a_recorded_run_stores_what_import_produced(tables):
    tables.rollup()
    con = tables.db()
    key = con.execute("SELECT journal_key FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    con.close()
    snap = json.loads((journal.state_dir(tables.path) / "state" / f"{key}.json").read_text())
    assert sorted(snap["modules"]["pkg/tables.py"]) == ["OTHER", "TABLE", "build", "lookup", "other"]
    assert snap["volatile"] == {"globals": {}, "classes": {}} and snap["parts"] == {}


def test_changed_module_level_data_selects_the_tests_that_reach_it(tables):
    tables.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    out = tables.cli("affected")
    # not everything, and not every test that touches the file: test_other
    # reads OTHER, which is what it was
    assert out["mode"] == "select" and out["run_all_reasons"] == []
    assert out["import_state"] == {
        "overrode": ["module-level change in import-time file: pkg/tables.py"],
        "trees": [{"globals": 1, "classes": 0, "holders": 0, "names": ["TABLE"]}],
    }
    assert {t: out_reason for t, out_reason in tables.select()["selected"].items()} == {
        T + "test_spawn": "starts a process prongs could not record",
        T + "test_git": "starts a process prongs could not record",
        "tests/test_t.py::test_lookup": "reaches import-time state that changed",
    }
    run = tables.cli("run", "--no-record", code=1)
    assert run["summary"]["failed"] == 1 and run["selection"]["mode"] == "select"


def test_a_changed_import_time_function_selects_by_what_it_built(tables):
    # build() runs while pkg/tables.py is imported: OTHER holds what it
    # returned, and test_other reads OTHER without ever entering build()
    tables.edit("pkg/mod.py", 'return {"v": 1}', 'return {"v": 3}')
    sel = tables.select()
    assert sel["mode"] == "select" and sel["import_state"] == {
        "overrode": ["executed at import/collection time: pkg/mod.py::build"],
        "trees": [{"globals": 1, "classes": 0, "holders": 0, "names": ["OTHER"]}],
    }
    assert {t.split("::")[-1]: why for t, why in sel["selected"].items()} == {
        "test_one": "touches changed function pkg/mod.py::build",
        "test_two": "touches changed function pkg/mod.py::build",
        "test_spawn": "starts a process prongs could not record",
        "test_git": "starts a process prongs could not record",
        "test_other": "reaches import-time state that changed",
    }
    assert tables.cli("run", "--no-record", code=1)["summary"]["failed"] == 3
    # a body change that leaves what import built as it was: only the tests
    # that enter the function
    tables.git("checkout", "--", "pkg/mod.py")
    tables.edit("pkg/mod.py", 'return {"v": 1}', 'return dict(v=1)')
    sel = tables.select()
    assert sel["import_state"]["trees"] == [
        {"globals": 0, "classes": 0, "holders": 0, "names": []}]
    assert "tests/test_t.py::test_other" not in sel["selected"]


def test_a_changed_class_selects_whoever_holds_it(tables):
    tables.write("pkg/shapes.py", "class Shape:\n    sides = 4\n\n\nDEFAULT = Shape()\n\n\n"
                 "def default_sides():\n    return DEFAULT.sides\n\n\ndef unrelated():\n    return 0\n")
    tables.write("tests/test_s.py", "from pkg import shapes\n\n\ndef test_default():\n"
                 "    assert shapes.default_sides() == 4\n\n\ndef test_unrelated():\n"
                 "    assert shapes.unrelated() == 0\n")
    tables.commit("shapes")
    assert tables.record().returncode == 0
    tables.edit("pkg/shapes.py", "sides = 4", "sides = 5")
    sel = tables.select()
    # DEFAULT is an instance of the changed class: default_sides() names it
    assert sel["import_state"]["trees"] == [
        {"globals": 0, "classes": 1, "holders": 1, "names": ["DEFAULT", "Shape"]}]
    assert sel["selected"]["tests/test_s.py::test_default"] == "reaches import-time state that changed"
    assert "tests/test_s.py::test_unrelated" not in sel["selected"]


def test_import_state_in_shadow_runs_everything_and_reports(tables):
    tables.write("pyproject.toml", tables.read("pyproject.toml").replace(
        "import_state = true", 'import_state = "shadow"\ninert = ["pyproject.toml"]'))
    tables.edit("pkg/mod.py", 'return {"v": 1}', 'return {"v": 3}')
    out = tables.cli("run", "--no-record", code=1)
    assert out["selection"]["mode"] == "run_all"
    assert out["shadow"]["status_changes"] == 3
    rules = out["shadow"]["rules"]
    assert rules["import_state"]["n_missed"] == 0
    # without it, selecting would have skipped the test that reads OTHER
    assert rules["selection"]["missed"] == ["tests/test_t.py::test_other"]


def test_a_tree_that_does_not_import_runs_everything(tables):
    tables.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 1}[0]")
    sel = tables.select()
    assert sel["mode"] == "run_all" and sel["reasons"] == [
        "module-level change in import-time file: pkg/tables.py",
        "import check failed: collecting the suite exited 2"]


def snapshots(repo) -> list[str]:
    return sorted(p.stem for p in (journal.state_dir(repo.path) / "state").glob("*.json"))


def test_selection_and_skip_counts_cover_every_test(tables):
    tables.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    sel = tables.select()
    assert (sel["n_selected"], sel["n_skipped"], sel["n_total"]) == (3, 7, 10)


def test_a_partial_run_snapshots_the_whole_suite(tables):
    tables.rollup()
    full = snapshots(tables)
    # one test of one module: the snapshot still holds what the other test
    # module imports, so a later change there can be compared with it
    assert tables.record("tests/test_e.py::test_plain").returncode == 0
    (new,) = set(snapshots(tables)) - set(full)
    snap = json.loads((journal.state_dir(tables.path) / "state" / f"{new}.json").read_text())
    assert sorted(snap["modules"]) == [
        "pkg/__init__.py", "pkg/mod.py", "pkg/tables.py", "tests/test_e.py", "tests/test_t.py"]
    tables.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    sel = tables.select()
    assert sel["mode"] == "select"
    assert sel["selected"]["tests/test_t.py::test_lookup"] == "reaches import-time state that changed"


def test_a_module_no_snapshot_covers_runs_everything(tables):
    tables.rollup()
    (key,) = snapshots(tables)
    path = journal.state_dir(tables.path) / "state" / f"{key}.json"
    snap = json.loads(path.read_text())
    del snap["modules"]["pkg/tables.py"]
    path.write_text(json.dumps(snap))
    tables.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    sel = tables.select()
    assert sel["mode"] == "run_all" and sel["reasons"] == [
        "module-level change in import-time file: pkg/tables.py",
        "no import-state snapshot covers pkg/tables.py"]


def test_snapshots_of_runs_that_are_no_longer_evidence_are_deleted(tables):
    tables.rollup()
    (first,) = snapshots(tables)
    assert tables.record().returncode == 0  # a newer full run: the evidence now
    assert len(snapshots(tables)) == 2
    tables.select()
    (kept,) = snapshots(tables)
    assert kept != first


def test_snapshots_travel_with_the_ci_artifacts(tables, tmp_path):
    import subprocess

    from conftest import Repo

    tables.rollup()
    (key,) = snapshots(tables)
    art = tmp_path / "artifact"
    assert tables.cli("ci", "save", str(art))["states"] == [key]
    assert json.loads((art / "manifest.json").read_text())["states"] == [key]
    subprocess.run(["git", "clone", "-q", tables.path.as_uri(), str(tmp_path / "job")],
                   check=True, capture_output=True)
    job = Repo(tmp_path / "job")
    assert job.cli("ci", "restore", str(art))["states"] == [key]
    assert snapshots(job) == [key]
    # a pull request that changes import-time code: selected in the fresh
    # clone, from the snapshot the default branch's job recorded
    job.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    out = job.cli("affected")
    assert out["mode"] == "select" and out["run_all_reasons"] == []
    assert "tests/test_t.py::test_lookup" in out["tests"]
    assert "tests/test_t.py::test_other" not in out["tests"]
    # and without it, everything
    for f in (journal.state_dir(job.path) / "state").glob("*.json"):
        f.unlink()
    assert job.cli("affected")["mode"] == "run_all"


def test_an_environment_variable_a_test_leaves_set_is_not_in_the_snapshot(tables):
    # the snapshot is taken when the session ends, in a fresh process: with
    # what the tests did to os.environ it would describe another import
    # than the one a later selection compares it with
    tables.write("pkg/mode.py", "import os\n\nMODE = os.environ.get('PKG_MODE', 'plain')\n\n\n"
                 "def mode():\n    return MODE\n")
    tables.write("tests/test_mode.py", "import os\n\nfrom pkg import mode\n\n\n"
                 "def test_mode():\n    assert mode.mode() == 'plain'\n\n\n"
                 "def test_leaks():\n    os.environ['PKG_MODE'] = 'leaked'\n")
    tables.commit("a test that leaves the environment changed")
    assert tables.record().returncode == 0
    tables.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    sel = tables.select()
    assert sel["import_state"]["trees"] == [
        {"globals": 1, "classes": 0, "holders": 0, "names": ["TABLE"]}]
    assert "tests/test_mode.py::test_mode" not in sel["selected"]


def test_an_explicit_base_the_map_was_recorded_at_selects_by_import_state_too(tables):
    base = tables.head()
    tables.edit("pkg/tables.py", "TABLE = {'k': 1}", "TABLE = {'k': 2}")
    tables.commit("a change to import-time data")
    sel = tables.select(base=base)
    assert sel["mode"] == "select" and sel["import_state"]["trees"] == [
        {"globals": 1, "classes": 0, "holders": 0, "names": ["TABLE"]}]
    assert sel["selected"]["tests/test_t.py::test_lookup"] == "reaches import-time state that changed"
    assert "tests/test_t.py::test_other" not in sel["selected"]
    # a base no recorded run saw: nothing to compare with
    tables.edit("pkg/tables.py", "TABLE = {'k': 2}", "TABLE = {'k': 3}")
    assert tables.select(base="HEAD")["mode"] == "run_all"


def test_a_child_that_is_killed_still_says_what_it_ran(project, tmp_path):
    # a server a test starts and kills never gets to exit: what it entered
    # is written as it goes, not when it ends
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "sitecustomize.py").write_text(
        "import os\nif os.environ.get('PRONGS_CHILD'):\n    import prongs.child\n")
    project.env["PYTHONPATH"] = str(tmp_path / "site")
    project.env["PRONGS_RECORD_CHILDREN"] = "1"
    project.write("pkg/server.py", "import os\nimport signal\nimport subprocess\nimport sys\n\n\n"
                  "def serve():\n    os.kill(os.getpid(), signal.SIGKILL)\n\n\n"
                  "def start():\n    code = 'from pkg.server import serve; serve()'\n"
                  "    return subprocess.run([sys.executable, '-c', code]).returncode\n")
    project.write("tests/test_k.py", "import signal\n\nfrom pkg import server\n\n\n"
                  "def test_killed():\n    assert server.start() == -signal.SIGKILL\n")
    project.commit("a child that is killed")
    assert project.record().returncode == 0
    project.rollup()
    con = project.db()
    deps = {(p, q) for p, q in con.execute(
        "SELECT f.path, fn.qualname FROM tests t JOIN current_links l ON l.test_id = t.id "
        "JOIN funcs fn ON fn.id = l.func_id JOIN files f ON f.id = fn.file_id "
        "WHERE t.test_id = ?", ("tests/test_k.py::test_killed",))}
    con.close()
    assert deps == {("pkg/__init__.py", "<module>"), ("pkg/server.py", "<module>"),
                    ("pkg/server.py", "serve"), ("pkg/server.py", "start"),
                    ("tests/test_k.py", "test_killed")}
