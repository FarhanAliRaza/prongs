"""Milestone 1 exit gate: the prefork prototype must match vanilla pytest on
the compatibility corpus — same collected node IDs, same phase outcomes, same
skip/xfail states, same failure type + location, same exit code, no missing
or duplicated tests — across worker counts and batch sizes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ZTEST_ROOT = Path(__file__).resolve().parents[2]
CORPUS = ZTEST_ROOT / "tests" / "compatibility" / "corpus"
PYTHON_DIR = ZTEST_ROOT / "python"

sys.path.insert(0, str(PYTHON_DIR))

from ztest_py.reporting import compare_oracles  # noqa: E402


def run_with_oracle(tmp_path: Path, name: str, runner_args: list[str]) -> dict:
    out = tmp_path / f"{name}.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYTHON_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, *runner_args, str(CORPUS), "-q",
         "-p", "ztest_py.oracle", f"--ztest-oracle-out={out}",
         "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        env=env,
        cwd=ZTEST_ROOT,
        timeout=300,
    )
    assert out.exists(), (
        f"{name} produced no oracle file\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    doc = json.loads(out.read_text())
    doc["_process_exit"] = proc.returncode
    return doc


@pytest.fixture(scope="module")
def vanilla(tmp_path_factory) -> dict:
    tmp = tmp_path_factory.mktemp("vanilla")
    doc = run_with_oracle(tmp, "vanilla", ["-m", "pytest"])
    # Guard against configuration accidentally hiding the corpus: an
    # empty-vs-empty comparison would vacuously pass every parity test.
    assert len(doc["collected"]) >= 40, "corpus was not collected"
    return doc


@pytest.mark.parametrize("jobs", [1, 2, 8])
def test_parity_across_worker_counts(vanilla: dict, tmp_path: Path, jobs: int) -> None:
    proto = run_with_oracle(
        tmp_path, f"proto{jobs}", ["-m", "ztest_py", "run", "-j", str(jobs), "--"]
    )
    problems = compare_oracles(vanilla, proto)
    assert problems == []
    assert proto["_process_exit"] == vanilla["_process_exit"]


@pytest.mark.parametrize("batch", [2, 5, 16])
def test_parity_across_batch_sizes(vanilla: dict, tmp_path: Path, batch: int) -> None:
    proto = run_with_oracle(
        tmp_path,
        f"batch{batch}",
        ["-m", "ztest_py", "run", "-j", "2", "--initial-batch", str(batch), "--"],
    )
    problems = compare_oracles(vanilla, proto)
    assert problems == []


def test_every_test_runs_exactly_once(vanilla: dict, tmp_path: Path) -> None:
    proto = run_with_oracle(tmp_path, "once", ["-m", "ztest_py", "run", "-j", "4", "--"])
    assert sorted(proto["collected"]) == sorted(vanilla["collected"])
    assert proto["duplicates"] == []
    executed = set(proto["tests"])
    assert executed == set(vanilla["tests"])
