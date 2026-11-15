"""watch.py: with the kernel reporting which files every process opens, a
test that starts a program prongs cannot see into depends on what was
opened, and a file nothing opened is a file no test read."""

from __future__ import annotations

import sys

import pytest

from prongs import provenance

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="inotify")

MOD = '''import os
import subprocess
import sys


def build():
    return {"v": 1}


def child_only():
    return 7


def spawn():
    code = "from pkg.mod import child_only; print(child_only())"
    return int(subprocess.check_output([sys.executable, "-c", code]))


def git_version():
    return subprocess.check_output(["git", "--version"]).decode()


def head():
    return subprocess.check_output(["git", "rev-parse", "HEAD"]).decode()


def cat():
    return subprocess.check_output(["cat", "data/table.json"]).decode()


def listing():
    return sorted(os.listdir("data"))


def unrelated():
    return 0
'''
TESTS = '''from pkg import mod


def test_build():
    assert mod.build() == {"v": 1}


def test_spawn():
    assert mod.spawn() == 7


def test_git():
    assert mod.git_version().startswith("git version")


def test_head():
    assert len(mod.head().strip()) == 40


def test_cat():
    assert mod.cat() == '{"k": 1}\\n'


def test_listing():
    assert mod.listing() == ["table.json", "unused.json"]


def test_plain():
    assert mod.unrelated() == 0
'''
T = "tests/test_w.py::"
MARKER = "starts a process prongs could not record"


@pytest.fixture
def watched(repo):
    repo.write("pkg/mod.py", MOD)
    repo.write("tests/test_w.py", TESTS)
    (repo.path / "tests/test_m.py").unlink()
    repo.write("data/table.json", '{"k": 1}\n')
    repo.write("data/unused.json", "{}\n")
    repo.write("assets/logo.svg", "<svg/>\n")
    repo.write("settings.cfg", "[tool]\n")
    repo.commit("project")
    del repo.env["PRONGS_WATCH"]
    # no recorder inside child processes: what they run is for the kernel to say
    repo.env["PRONGS_RECORD_CHILDREN"] = "0"
    p = repo.record()
    assert p.returncode == 0, p.stdout + p.stderr
    return repo


def selected(repo) -> dict[str, str]:
    sel = repo.select()
    assert sel["mode"] == "select", sel.get("reasons")
    return {t.removeprefix(T): why for t, why in sel["selected"].items()}


def test_a_watched_run_says_so(watched):
    watched.rollup()
    ((recorder,),) = watched.db().execute("SELECT recorder FROM runs").fetchall()
    assert provenance.fingerprint_field(recorder, "watch") == ""
    assert provenance.fingerprint_field(recorder, "env") == provenance.environment(str(watched.path))


def test_a_code_change_selects_the_processes_that_opened_the_module(watched):
    watched.edit("pkg/mod.py", 'return {"v": 1}', 'return {"v": 2}')
    # test_git and test_cat start programs that open no module; test_head's
    # read the repository's own state, which no list of files describes
    assert selected(watched) == {
        "test_build": "touches changed function pkg/mod.py::build",
        "test_spawn": "starts a process that opens changed module pkg/mod.py",
        "test_head": MARKER,
    }


def test_a_file_another_program_read_selects_the_test_that_started_it(watched):
    watched.edit("data/table.json", '{"k": 1}', '{"k": 2}')
    assert selected(watched) == {
        "test_cat": "reads changed file data/table.json",
        "test_head": MARKER,
    }


def test_a_file_no_process_opened_selects_only_what_cannot_be_vouched_for(watched):
    watched.edit("assets/logo.svg", "<svg/>", "<svg></svg>")
    sel = watched.select()
    assert sel["mode"] == "select" and sel["units"]["unread"] == ["assets/logo.svg"]
    assert selected(watched) == {"test_head": MARKER}


def test_a_file_read_before_any_test_starts_still_runs_everything(watched):
    watched.edit("settings.cfg", "[tool]", "[tools]")
    sel = watched.select()
    assert sel["mode"] == "run_all" and sel["reasons"] == ["non-Python file changed: settings.cfg"]


def test_a_new_file_selects_the_tests_that_listed_its_directory(watched):
    watched.write("data/extra.json", "{}\n")
    watched.commit("a new data file")
    assert selected(watched) == {
        "test_listing": "lists directory data/",
        "test_head": MARKER,
    }
    watched.git("rm", "-q", "data/extra.json", "data/unused.json")
    assert selected(watched) == {
        "test_listing": "lists directory data/",
        "test_head": MARKER,
    }


def test_without_the_watcher_an_unread_file_runs_everything(watched):
    watched.env["PRONGS_WATCH"] = "0"
    assert watched.record().returncode == 0
    watched.edit("assets/logo.svg", "<svg/>", "<svg></svg>")
    sel = watched.select()
    assert sel["mode"] == "run_all" and sel["reasons"] == [
        "non-Python file changed: assets/logo.svg"]
