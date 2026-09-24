"""Agent-facing CLI (PoC Phase 5).

  python -m fastest affected [--base REV]             what would run, and why — JSON
  python -m fastest run [--base REV] [--no-record]    select + execute + JSON results
  python -m fastest daemon                            start the warm daemon
  python -m fastest stop                              stop the daemon

Run from the repo root (same directory you'd run pytest from).
`run` uses the warm daemon when its socket exists, else falls back to
spawning pytest. Output is a single JSON document on stdout, built for a
token budget: tracebacks truncated, skip receipts aggregated by reason.

Every result carries `evidence`: which runs and commits the map comes from,
how far behind HEAD it is, and the trees the diff was taken against (the
evidence's own, unless --base is given). A run is appended to the journal as
a partial run, refreshing exactly the tests it executed, unless --no-record;
its summary reports selected vs ran vs recorded (`unrun_targets`,
`record_ok`, `complete`) so a run that silently did less than asked is
visible.
"""

from __future__ import annotations

import json
import os
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
    from fastest.daemon import ResultCollector  # reuse the schema

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
    resp = {
        "exit": int(code),
        "wall_s": round(time.monotonic() - t0, 4),
        "results": collector.results,
    }
    if code not in (0, 1):  # usage/internal error: attach output, as the daemon does
        resp["pytest_output"] = buf.getvalue()[-3000:]
    return resp


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
        out["summary"] = {"status": "nothing_to_run", "wall_s": round(time.monotonic() - t0, 3)}
        return out

    extra = ["--fastest-cov"] if args.record else []
    before = latest_recorded_run(repo) if args.record else None
    resp = run_via_daemon(aff["targets"], extra)
    out["executor"] = "daemon" if resp else "subprocess"
    if resp and (resp.get("stale") or resp.get("exit") in (2, 3, 4)):
        # warm image unusable (stale beyond re-import, or infra error):
        # run cold — slower, never wrong. A file-watching daemon that
        # re-warms on save is the product fix.
        out["executor"] = "subprocess (warm image stale — restart daemon)"
        out["daemon_stale_errors"] = resp.get("errors") or resp.get("pytest_output", "")[-500:]
        resp = None
    if resp is None:
        resp = run_via_subprocess(aff["targets"], extra)
    failures = [r for r in resp.get("results", []) if r["status"] == "failed"]
    # agents need one traceback per root cause, not one per test: group by
    # the error's last line (type + message + assertion site)
    groups: dict[str, dict] = {}
    for f in failures:
        rep = f.get("longrepr", "")
        tail_lines = [ln for ln in rep.strip().splitlines() if ln.strip()]
        fp = tail_lines[-1][:200] if tail_lines else "unknown"
        g = groups.setdefault(fp, {
            "error": fp,
            "representative": f["id"],
            "longrepr": rep[-1500:],
            "also_failed": [],
        })
        if g["representative"] != f["id"]:
            g["also_failed"].append(f["id"])
    out["failures"] = list(groups.values())  # passes are summarized, not listed
    results = resp.get("results", [])
    ran = len(results)
    # in == out, part 1: every target must have produced a result. A node id
    # that no longer exists or a module skipped at collection runs nothing,
    # and "0 failed" must not read as "verified".
    ran_ids = {r["id"] for r in results}

    def produced_results(target: str) -> bool:
        if "::" in target:
            return target in ran_ids
        return any(i.startswith(target + "::") for i in ran_ids)  # whole-file target

    unrun = [t for t in aff["targets"] if not produced_results(t)]
    out["summary"] = {
        "status": "failed" if failures else ("incomplete" if unrun else "passed"),
        "passed": sum(1 for r in results if r["status"] == "passed"),
        "failed": len(failures),
        "ran": ran,
        "unrun_targets": unrun,
        "complete": not unrun and resp.get("exit") in (0, 1),
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


def main():
    import argparse

    ap = argparse.ArgumentParser(prog="fastest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("affected", "run"):
        p = sub.add_parser(name)
        p.add_argument("--base", default=None,
                       help="diff base (default: the commits the evidence was observed at)")
        if name == "run":
            p.add_argument("--record", action=argparse.BooleanOptionalAction, default=True,
                           help="append this run to the journal, refreshing the tests it "
                                "runs (default: on)")
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


if __name__ == "__main__":
    main()
