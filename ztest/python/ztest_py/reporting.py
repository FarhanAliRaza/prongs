"""Report serialization helpers and the correctness oracle (Milestone 0).

The oracle records, for every test, the outcome of each runtest phase plus a
failure fingerprint that is stable across runners: failure *type* and *source
location*, never the exact rendered longrepr (paths, timing and ordering vary
legitimately between runners).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import pytest


def serialize_report(config: pytest.Config, report: pytest.TestReport) -> dict[str, Any]:
    data = config.hook.pytest_report_to_serializable(config=config, report=report)
    assert data is not None, "pytest_report_to_serializable returned None"
    return data


def deserialize_report(config: pytest.Config, data: dict[str, Any]) -> pytest.TestReport:
    report = config.hook.pytest_report_from_serializable(config=config, data=data)
    assert report is not None, "pytest_report_from_serializable returned None"
    return report


def failure_fingerprint(report: pytest.TestReport) -> dict[str, Any] | None:
    """A runner-independent identity for a failure: type + source location."""
    if report.passed:
        return None
    longrepr = report.longrepr
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None:
        first_line = crash.message.split("\n", 1)[0]
        failure_type = first_line.split(":", 1)[0].strip()
        return {
            "type": failure_type,
            "path": os.path.basename(str(crash.path)),
            "lineno": crash.lineno,
        }
    # skip/xfail: (path, lineno, reason); a serialization roundtrip turns the
    # tuple into a list, so accept both.
    if isinstance(longrepr, (tuple, list)) and len(longrepr) == 3:
        path, lineno, reason = longrepr
        return {
            "type": "skip",
            "path": os.path.basename(str(path)),
            "reason_kind": reason.split(":", 1)[0],
        }
    if longrepr is not None:
        return {"type": "other", "text_head": str(longrepr).split("\n", 1)[0][:80]}
    return {"type": "unknown"}


def fingerprint_digest(fp: dict[str, Any] | None) -> str | None:
    if fp is None:
        return None
    blob = json.dumps(fp, sort_keys=True).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class OraclePlugin:
    """Collects per-test phase outcomes for cross-runner comparison.

    Works in any runner that fires the standard logreport hooks — vanilla
    pytest, the prefork prototype's replay path, and (later) the ztest
    reporting host.
    """

    def __init__(self, out_path: str) -> None:
        self.out_path = out_path
        self.records: dict[str, dict[str, Any]] = {}
        self.duplicates: list[str] = []
        self.collected_nodeids: list[str] = []
        self.exitstatus: int | None = None

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected_nodeids = [item.nodeid for item in session.items]

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        record = self.records.setdefault(
            report.nodeid,
            {"nodeid": report.nodeid, "phases": {}, "duration_ns": 0},
        )
        if report.when in record["phases"]:
            self.duplicates.append(f"{report.nodeid}:{report.when}")
        record["phases"][report.when] = {
            "outcome": report.outcome,
            "wasxfail": getattr(report, "wasxfail", None) is not None
            and getattr(report, "wasxfail", None) != "",
            "fingerprint": failure_fingerprint(report),
        }
        record["duration_ns"] += int(report.duration * 1e9)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        self.exitstatus = int(exitstatus)
        payload = {
            "collected": self.collected_nodeids,
            "exitstatus": self.exitstatus,
            "duplicates": self.duplicates,
            "tests": self.records,
        }
        os.makedirs(os.path.dirname(os.path.abspath(self.out_path)), exist_ok=True)
        with open(self.out_path, "w") as f:
            json.dump(payload, f, indent=1, sort_keys=True)


def compare_oracles(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Compare two oracle documents; return a list of human-readable diffs.

    Compared: collected node IDs (as sets and counts), phase outcomes,
    skip/xfail states, failure type + location, exit status, duplicates.
    Not compared: durations, rendered failure text, execution order.
    """
    problems: list[str] = []

    ca, cb = a["collected"], b["collected"]
    if sorted(ca) != sorted(cb):
        only_a = sorted(set(ca) - set(cb))
        only_b = sorted(set(cb) - set(ca))
        problems.append(f"collected sets differ: only_a={only_a[:5]} only_b={only_b[:5]}")
    if len(ca) != len(cb):
        problems.append(f"collected counts differ: {len(ca)} vs {len(cb)}")

    if a["exitstatus"] != b["exitstatus"]:
        problems.append(f"exit status differs: {a['exitstatus']} vs {b['exitstatus']}")
    if a["duplicates"]:
        problems.append(f"duplicate phase reports in A: {a['duplicates'][:5]}")
    if b["duplicates"]:
        problems.append(f"duplicate phase reports in B: {b['duplicates'][:5]}")

    ta, tb = a["tests"], b["tests"]
    for nodeid in sorted(set(ta) | set(tb)):
        if nodeid not in ta:
            problems.append(f"{nodeid}: missing from A")
            continue
        if nodeid not in tb:
            problems.append(f"{nodeid}: missing from B")
            continue
        pa, pb = ta[nodeid]["phases"], tb[nodeid]["phases"]
        for phase in ("setup", "call", "teardown"):
            ra, rb = pa.get(phase), pb.get(phase)
            if (ra is None) != (rb is None):
                problems.append(f"{nodeid}: phase {phase} present only in one runner")
                continue
            if ra is None:
                continue
            if ra["outcome"] != rb["outcome"]:
                problems.append(
                    f"{nodeid}: {phase} outcome {ra['outcome']} vs {rb['outcome']}"
                )
            if ra["wasxfail"] != rb["wasxfail"]:
                problems.append(f"{nodeid}: {phase} wasxfail differs")
            fa, fb = ra["fingerprint"], rb["fingerprint"]
            if (fa is None) != (fb is None):
                problems.append(f"{nodeid}: {phase} failure presence differs")
            elif fa is not None and (
                fa.get("type") != fb.get("type") or fa.get("path") != fb.get("path")
            ):
                problems.append(f"{nodeid}: {phase} fingerprint {fa} vs {fb}")
    return problems
