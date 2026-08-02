"""Milestone 3 exit gate: `ztest run -j 2 -- tests/` must collect once,
start workers, run every test exactly once, print failures, and exit with
pytest-equivalent status. Skipped when the Zig binary has not been built
(`python -m ziglang build` or `zig build` in ztest/).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ZTEST_ROOT = Path(__file__).resolve().parents[2]
BINARY = ZTEST_ROOT / "zig-out" / "bin" / "ztest"
CORPUS = ZTEST_ROOT / "tests" / "compatibility" / "corpus"
PYTHON_DIR = ZTEST_ROOT / "python"

pytestmark = pytest.mark.skipif(
    not BINARY.exists(), reason="ztest binary not built (zig build)"
)


def run_ztest(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYTHON_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [str(BINARY), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=ZTEST_ROOT,
        timeout=300,
    )


def summary_counts(stdout: str) -> dict[str, int]:
    match = re.search(
        r"===== (\d+) tests: (\d+) passed, (\d+) failed, (\d+) errors, "
        r"(\d+) skipped, (\d+) xfailed, (\d+) xpassed",
        stdout,
    )
    assert match, f"no summary line in output:\n{stdout[-2000:]}"
    keys = ["total", "passed", "failed", "errors", "skipped", "xfailed", "xpassed"]
    return dict(zip(keys, map(int, match.groups())))


def test_corpus_summary_matches_pytest() -> None:
    proc = run_ztest(
        ["run", "-j", "2", "--python", sys.executable, "--",
         str(CORPUS), "-q", "-p", "no:cacheprovider"]
    )
    counts = summary_counts(proc.stdout)
    # Reference counts from vanilla pytest on the corpus.
    assert counts == {
        "total": 45,
        "passed": 34,
        "failed": 6,
        "errors": 2,
        "skipped": 2,
        "xfailed": 1,
        "xpassed": 1,
    }
    assert proc.returncode == 1  # failures present


def test_all_passing_run_exits_zero(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_ok.py").write_text(
        "\n".join(f"def test_{i}():\n    assert True\n" for i in range(20))
    )
    proc = run_ztest(
        ["run", "-j", "2", "--python", sys.executable, "--",
         str(suite), "-q", "-p", "no:cacheprovider"]
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    counts = summary_counts(proc.stdout)
    assert counts["total"] == 20
    assert counts["passed"] == 20


def test_worker_crash_is_recovered(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_crash.py").write_text(
        "import os\n"
        "def test_a():\n    assert True\n"
        "def test_boom():\n    os._exit(77)\n"
        "def test_b():\n    assert True\n"
        "def test_c():\n    assert True\n"
    )
    proc = run_ztest(
        ["run", "-j", "2", "--python", sys.executable, "--",
         str(suite), "-q", "-p", "no:cacheprovider"]
    )
    assert proc.returncode == 1
    assert "CRASHED" in proc.stdout
    counts = summary_counts(proc.stdout)
    assert counts["total"] == 4
    assert counts["passed"] == 3
