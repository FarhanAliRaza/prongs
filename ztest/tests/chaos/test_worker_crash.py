"""Chaos: a test that kills its worker process outright must not hang the
controller, lose unrelated results, or run twice without a recorded attempt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ZTEST_ROOT = Path(__file__).resolve().parents[2]
PYTHON_DIR = ZTEST_ROOT / "python"

sys.path.insert(0, str(PYTHON_DIR))


CRASHY_SUITE = textwrap.dedent(
    """
    import os

    def test_before():
        assert True

    def test_crashes_worker():
        os._exit(77)  # simulate a segfault-style death

    def test_after_one():
        assert True

    def test_after_two():
        assert True
    """
)


def test_crash_recovery(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_crashy.py").write_text(CRASHY_SUITE)
    oracle_out = tmp_path / "oracle.json"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYTHON_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [
            sys.executable, "-m", "ztest_py", "run", "-j", "2", "--",
            str(suite), "-q",
            "-p", "ztest_py.oracle", f"--ztest-oracle-out={oracle_out}",
            "-p", "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,  # the real assertion: no hang
    )
    assert proc.returncode != 0
    doc = json.loads(oracle_out.read_text())

    tests = doc["tests"]
    # Every test has a recorded outcome, including the crasher.
    assert len(doc["collected"]) == 4
    assert set(tests) == set(doc["collected"])
    crash_record = tests[[k for k in tests if "crashes_worker" in k][0]]
    assert crash_record["phases"]["call"]["outcome"] == "failed"
    # Unrelated tests still pass.
    passed = [
        k for k, v in tests.items()
        if v["phases"].get("call", {}).get("outcome") == "passed"
    ]
    assert len(passed) == 3
