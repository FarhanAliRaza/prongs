"""Agent-facing CLI (PoC Phase 5).

  python -m fastest affected [--base REV]             what would run, and why — JSON
  python -m fastest run [--base REV] [--no-record]    select + execute + JSON results
  python -m fastest audit [--base REV]                select, then run everything and
                                                      report every status change the
                                                      selection skipped, with receipts
  python -m fastest daemon                            start the warm daemon
  python -m fastest stop                              stop the daemon

Run from the repo root (same directory you'd run pytest from).
`run` uses the warm daemon when its socket exists, else falls back to
spawning pytest. Output is a single JSON document on stdout, built for a
token budget: tracebacks truncated, failures grouped by exception line, skip
receipts aggregated by reason.

Every result carries `evidence`: which runs and commits the map comes from,
how far behind HEAD it is, and the trees the diff was taken against (the
evidence's own, unless --base is given). A run is appended to the journal as
a partial run, refreshing exactly the tests it executed, unless --no-record.
Every run result carries a `conservation` block — collected, selected,
run_all, skipped and executed counts, and whether they balance — and a
status that is 'error' when pytest exits 2-5 or a module fails to collect,
'inconsistent' when the counts do not balance, never 'passed' for a run
that silently did less than asked.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from fastest.select import select


def cmd_affected(args) -> dict:
    repo = Path.cwd()
    db = repo / ".fastest" / "map.sqlite"
    if not db.exists():
        return {
            "error": "no coverage map",
            "hint": "build one with: pytest --fastest-cov",
        }
    sel = select(db, repo, args.base)
    if "error" in sel:
        return sel
    reasons = Counter(v.split(":")[0].split(" (")[0] for v in sel["selected"].values())
    return {
        "mode": sel["mode"],
        "n_total": sel["n_total"],
        "n_selected": len(sel["selected"]),
        "n_skipped": sel.get("n_skipped", 0),
        "changed_functions": sel.get("changed_functions", []),
        "run_all_reasons": sel.get("reasons", []),
        "selected_by_reason": dict(reasons),
        "tests": sorted(sel["selected"]),
        "selected_files": sel.get("selected_files", {}),
        "vanished": sel.get("vanished", []),
        "targets": sel["targets"],
        "evidence": sel["evidence"],
        "skip_receipt": skip_receipt(sel),
    }


def skip_receipt(sel: dict) -> dict:
    """The auditable basis for every skip: the rule, and how fresh the
    evidence behind it is. Not a proof — a transparent conservative policy."""
    ev = sel["evidence"]
    if sel["mode"] == "run_all":
        return {"skipped": 0, "rule": "nothing skipped: " + "; ".join(sel["reasons"])}
    return {
        "skipped": sel.get("n_skipped", 0),
        "rule": "a test is skipped only when, relative to the tree of the run that last "
                "observed it, no changed function or file is in its recorded dependency "
                "set; unmapped, new and statically-unfound tests always run",
        "evidence": {
            "runs": ev["contributing_runs"],
            "commits": [c[:8] for c in ev["evidence_commits"]],
            "commits_behind_head": ev["commits_behind"],
            "last_full_run": ev["last_full_run"]["id"] if ev["last_full_run"] else None,
            "tests_refreshed_since_full": ev["tests_refreshed_since_full"],
            "unchanged_since_observation": ev["dirty_rule"]["unchanged_since_observation"],
        },
        "warnings": ev["warnings"],
    }


def run_via_daemon(targets: list[str], extra_args: list[str]) -> dict | None:
    from fastest.daemon import SOCK, recv_msg, send_msg
    import socket

    if not os.path.exists(SOCK):
        return None
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(SOCK)
        send_msg(conn, {"op": "run", "node_ids": targets, "args": extra_args})
        resp = recv_msg(conn)
        conn.close()
        return resp
    except (ConnectionRefusedError, FileNotFoundError):
        return None


def run_via_subprocess(targets: list[str], extra_args: list[str]) -> dict:
    from fastest.daemon import ResultCollector, execution_response  # the daemon's schema

    import io

    import pytest

    collector = ResultCollector()
    t0 = time.monotonic()
    buf = io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf  # stdout is the JSON document; nothing else may print
    try:
        code = pytest.main(
            targets + extra_args + ["-q", "--no-header", "-p", "no:cacheprovider"],
            plugins=[collector],
        )
    finally:
        sys.stdout, sys.stderr = saved
    # exit 2-5 is an error result, exactly as from the daemon: a collection
    # failure that ran nothing must never read as "passed"
    return execution_response(code, time.monotonic() - t0, collector, buf.getvalue)


_HEX = re.compile(r"0x[0-9a-fA-F]+")


def exception_line(result: dict) -> str:
    """The line that names a failure: pytest's crash message (what its short
    summary prints), else the first line of the traceback's last `E` block.
    Never the traceback's last line: under --tb=short that is the same
    'Use -v to get more diff' hint for every truncated assertion."""
    line = result.get("crash") or ""
    if not line:
        lines = result.get("longrepr", "").splitlines()
        block_start, in_block = None, False
        for ln in lines:
            is_e = ln == "E" or ln.startswith("E ")
            if is_e and not in_block:
                block_start = ln[1:]
            in_block = is_e
        if block_start is not None:
            line = block_start
        else:
            rest = [ln for ln in lines if ln.strip()]
            line = rest[-1] if rest else ""
    return _HEX.sub("0x?", " ".join(line.split()))[:200] or "unknown"


def group_failures(failures: list[dict]) -> list[dict]:
    """Agents need one traceback per root cause, not one per test: group
    failures by their exception line (type + message)."""
    groups: dict[str, dict] = {}
    for f in failures:
        fp = exception_line(f)
        g = groups.setdefault(fp, {
            "error": fp,
            "representative": f["id"],
            "longrepr": f.get("longrepr", "")[-1500:],
            "also_failed": [],
        })
        if g["representative"] != f["id"]:
            g["also_failed"].append(f["id"])
    return list(groups.values())


def conservation(aff: dict, results: list[dict]) -> dict:
    """in == out, as arithmetic, in every run result.

    The selector's books: every live test in the map (`collected`) is either
    selected by a rule, selected because a run-all rule fired, or skipped
    with a receipt — collected == selected + run_all + skipped. The
    executor's books: every selected and run-all test produced a result
    (`executed` counts results, one per test id), except those the selector
    already knows are gone from a changed test module (`vanished`). Tests
    that ran unplanned (new tests in a whole-file target) are counted, never
    absorbed. Any mismatch makes `conserved` false and the run
    'inconsistent' rather than 'passed'."""
    planned = set(aff["tests"])
    run_all = len(planned) if aff["mode"] == "run_all" else 0
    block = {
        "collected": aff["n_total"],
        "selected": len(planned) - run_all,
        "run_all": run_all,
        "skipped": aff["n_skipped"],
        "executed": len(results),
    }
    ids = [r["id"] for r in results]
    ran = set(ids)
    vanished = set(aff.get("vanished", []))
    not_executed = sorted(planned - ran - vanished)
    books = block["collected"] == block["selected"] + block["run_all"] + block["skipped"]
    block["conserved"] = books and not not_executed and len(ran) == len(ids)
    if not books:
        block["mismatch"] = (
            f"collected {block['collected']} != selected {block['selected']} "
            f"+ run_all {block['run_all']} + skipped {block['skipped']}"
        )
    if not_executed:
        block["n_not_executed"] = len(not_executed)
        block["not_executed"] = not_executed[:20]
    if len(ran) != len(ids):
        block["duplicate_results"] = len(ids) - len(ran)
    if vanished:
        block["vanished"] = len(vanished)
    if ran - planned:
        block["unplanned"] = len(ran - planned)
    return block


def latest_recorded_run(repo: Path) -> dict | None:
    from fastest import mapdb

    con = mapdb.connect(str(repo / ".fastest" / "map.sqlite"))
    try:
        runs = mapdb.runs(con)
    finally:
        con.close()
    return runs[-1] if runs else None


def cmd_run(args) -> dict:
    t0 = time.monotonic()
    repo = Path.cwd()
    aff = cmd_affected(args)
    if "error" in aff:
        return aff
    out = {
        "selection": {
            k: aff[k]
            for k in ("mode", "n_total", "n_selected", "n_skipped", "changed_functions",
                      "run_all_reasons", "selected_files")
        },
        "evidence": aff["evidence"],
        "skip_receipt": aff["skip_receipt"],
    }
    if not aff["targets"]:
        out["results"] = []
        out["conservation"] = conservation(aff, [])
        out["summary"] = {
            "status": "nothing_to_run" if out["conservation"]["conserved"] else "inconsistent",
            "wall_s": round(time.monotonic() - t0, 3),
        }
        return out

    extra = ["--fastest-cov"] if args.record else []
    before = latest_recorded_run(repo) if args.record else None
    resp = run_via_daemon(aff["targets"], extra)
    out["executor"] = "daemon" if resp else "subprocess"
    if resp and (resp.get("stale") or "error" in resp):
        # warm image unusable (stale beyond re-import, infra error, pytest
        # exit 2-5): run cold — slower, never wrong. A file-watching daemon
        # that re-warms on save is the product fix.
        out["executor"] = "subprocess (warm image stale — restart daemon)"
        out["daemon_stale_errors"] = (
            resp.get("errors") or resp.get("pytest_output", "")[-500:] or resp.get("error")
        )
        resp = None
    if resp is None:
        resp = run_via_subprocess(aff["targets"], extra)
    results = resp.get("results", [])
    failures = [r for r in results if r["status"] == "failed"]
    out["failures"] = group_failures(failures)  # passes are summarized, not listed
    ran = len(results)
    out["conservation"] = conservation(aff, results)
    error = resp.get("error")
    if resp.get("collect_errors"):
        error = error or f"{len(resp['collect_errors'])} collection error(s)"
        out["collect_errors"] = [
            {"id": e["id"], "longrepr": e["longrepr"][-1000:]} for e in resp["collect_errors"][:10]
        ]
    if resp.get("collect_skipped"):
        out["collect_skipped"] = resp["collect_skipped"][:20]  # modules skipped at import
    if error:
        out["error"] = error
    # in == out, part 1: every target must have produced a result. A node id
    # that no longer exists or a module skipped at collection runs nothing,
    # and "0 failed" must not read as "verified".
    ran_ids = {r["id"] for r in results}

    def produced_results(target: str) -> bool:
        if "::" in target:
            return target in ran_ids
        return any(i.startswith(target + "::") for i in ran_ids)  # whole-file target

    unrun = [t for t in aff["targets"] if not produced_results(t)]
    if error:
        status = "error"
    elif failures:
        status = "failed"
    elif not out["conservation"]["conserved"]:
        status = "inconsistent"
    else:
        status = "passed"
    out["summary"] = {
        "status": status,
        "passed": sum(1 for r in results if r["status"] == "passed"),
        "failed": len(failures),
        "ran": ran,
        "unrun_targets": unrun,
        "complete": not unrun and not error,
        "skipped_by_selection": aff["n_skipped"],
        "exec_wall_s": resp.get("wall_s"),
        "total_wall_s": round(time.monotonic() - t0, 3),
    }
    if "pytest_output" in resp:
        out["pytest_output"] = resp["pytest_output"]
    if args.record:
        # in == out, part 2: the run we executed must be the run the journal saw
        after = latest_recorded_run(repo)
        recorded = after if after and (before is None or after["id"] != before["id"]) else None
        out["summary"]["recorded"] = (
            {"run_id": recorded["id"], "scope": recorded["scope"],
             "n_observed": recorded["n_observed"]}
            if recorded else None
        )
        out["summary"]["record_ok"] = bool(recorded) and recorded["n_observed"] == ran
    return out


def cmd_audit(args) -> dict:
    from fastest.audit import audit

    repo = Path.cwd()
    if not (repo / ".fastest" / "map.sqlite").exists():
        return {"error": "no coverage map", "hint": "build one with: pytest --fastest-cov"}
    raw = args.pytest_args if args.pytest_args is not None else os.environ.get("FASTEST_RUN_ARGS", "")
    res = audit(repo, args.base, pytest_args=shlex.split(raw), isolate=args.isolate,
                record=args.record)
    res.pop("statuses", None)  # per-test statuses are for harnesses, not the agent
    return res


def main():
    import argparse

    ap = argparse.ArgumentParser(prog="fastest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("affected", "run", "audit"):
        p = sub.add_parser(name)
        p.add_argument("--base", default=None,
                       help="diff base (default: the commits the evidence was observed at)")
        if name == "run":
            p.add_argument("--record", action=argparse.BooleanOptionalAction, default=True,
                           help="append this run to the journal, refreshing the tests it "
                                "runs (default: on)")
        if name == "audit":
            p.add_argument("--pytest-args", default=None,
                           help="extra pytest arguments for the full run "
                                "(default: $FASTEST_RUN_ARGS)")
            p.add_argument("--isolate", action=argparse.BooleanOptionalAction, default=True,
                           help="re-run each miss alone to separate first-order misses "
                                "from second-order pollution (default: on)")
            p.add_argument("--record", action=argparse.BooleanOptionalAction, default=False,
                           help="append the full run to the journal instead of a temporary "
                                "database (default: off — an audit never feeds the map)")
    sub.add_parser("daemon")
    sub.add_parser("stop")
    args = ap.parse_args()

    if args.cmd == "daemon":
        from fastest.daemon import serve

        serve()
    elif args.cmd == "stop":
        from fastest.daemon import SOCK, send_msg, recv_msg
        import socket

        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(SOCK)
        send_msg(c, {"op": "stop"})
        recv_msg(c)
    elif args.cmd == "affected":
        print(json.dumps(cmd_affected(args), indent=2))
    elif args.cmd == "run":
        print(json.dumps(cmd_run(args), indent=2))
    elif args.cmd == "audit":
        print(json.dumps(cmd_audit(args), indent=2))


if __name__ == "__main__":
    main()
