"""A throwaway git project with a two-function package and three tests, plus
helpers to run the real recorder / CLI in it (subprocess, so pytest's global
state never leaks between the outer and inner sessions)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from bolttest import journal, mapdb
from bolttest.select import select

MOD = "def a():\n    return 1\n\ndef b():\n    return 2\n\ndef c():\n    return a() + b()\n"
TESTS = (
    "from pkg.mod import a, b, c\n\n"
    "def test_a():\n    assert a() == 1\n\n"
    "def test_b():\n    assert b() == 2\n\n"
    "class TestC:\n    def test_c(self):\n        assert c() == 3\n"
)
T_A, T_B, T_C = "tests/test_m.py::test_a", "tests/test_m.py::test_b", "tests/test_m.py::TestC::test_c"


class Repo:
    def __init__(self, path: Path, env: dict | None = None):
        self.path = path
        self.env = env or {}  # extra environment for the recorder and the CLI

    # --- files & git ---------------------------------------------------------
    def write(self, rel: str, text: str) -> None:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text()

    def edit(self, rel: str, old: str, new: str) -> None:
        src = self.read(rel)
        assert src.count(old) == 1, (rel, old)
        self.write(rel, src.replace(old, new))

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, check=True
        ).stdout.strip()

    def commit(self, msg: str = "wip") -> str:
        self.git("add", "-A")
        self.git("commit", "-qm", msg)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    # --- bolttest -------------------------------------------------------------
    def _run(self, *argv: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for var in ("PYTEST_ADDOPTS", "BOLTTEST_COV", "BOLTTEST_DIR", "BOLTTEST_JOURNAL",
                    "BOLTTEST_JOURNAL_KEY"):
            if var not in self.env:
                env.pop(var, None)
        env.update(self.env)
        return subprocess.run(
            [sys.executable, "-m", *argv], cwd=self.path, capture_output=True, text=True, env=env
        )

    def record(self, *args: str) -> subprocess.CompletedProcess:
        """Real pytest with the recorder on, in the repo."""
        return self._run("pytest", "--bolttest-cov", "-q", "-p", "no:cacheprovider", *args)

    def cli(self, *args: str, code: int = 0) -> dict:
        """The CLI's JSON result; the exit code (a CI gate) must be `code`."""
        p = self._run("bolttest", *args)
        assert p.returncode == code, (p.returncode, p.stderr, p.stdout[-2000:])
        return json.loads(p.stdout)

    def rollup(self) -> dict:
        """What the CLI does before every selection: fold pending journal files."""
        return mapdb.rollup(journal.map_path(self.path), journal.journal_dir(self.path))

    def journal_files(self, sub: str = "") -> list[str]:
        d = journal.journal_dir(self.path) / sub
        return sorted(p.name for p in d.glob("*.sqlite")) if d.is_dir() else []

    def select(self, base: str | None = None, head: str | None = None) -> dict:
        self.rollup()
        return select(journal.map_path(self.path), self.path, base, head)

    def db(self) -> sqlite3.Connection:
        return sqlite3.connect(journal.map_path(self.path))

    def runs(self) -> list[tuple]:
        self.rollup()
        con = self.db()
        try:
            return con.execute(
                "SELECT id, scope, commit_sha, dirty_files, tree_changed, n_collected, n_observed "
                "FROM runs ORDER BY id"
            ).fetchall()
        finally:
            con.close()

    def tests(self) -> dict[str, tuple]:
        self.rollup()
        con = self.db()
        try:
            return {
                r[0]: r[1:] for r in con.execute(
                    "SELECT test_id, status, last_run, retired_run, n_obs FROM tests"
                )
            }
        finally:
            con.close()


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "proj")
    r.write("pkg/__init__.py", "")
    r.write("pkg/mod.py", MOD)
    r.write("tests/test_m.py", TESTS)
    r.write("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    r.write(".gitignore", ".bolttest/\n__pycache__/\n")
    r.git("init", "-q")
    r.git("config", "user.email", "t@example.com")
    r.git("config", "user.name", "t")
    r.commit("init")
    return r


@pytest.fixture
def recorded(repo: Repo) -> Repo:
    """A repo with one full run recorded at its initial commit."""
    p = repo.record()
    assert p.returncode == 0, p.stdout + p.stderr
    return repo
