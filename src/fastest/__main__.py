"""Agent-facing CLI (PoC Phase 5).

  python -m fastest affected [--base REV]   what would run, and why — JSON
  python -m fastest run [--base REV]        select + execute + JSON results
  python -m fastest daemon                  start the warm daemon
  python -m fastest stop                    stop the daemon

Run from the repo root (same directory you'd run pytest from).
`run` uses the warm daemon when its socket exists, else falls back to
spawning pytest. Output is a single JSON document on stdout, built for a
token budget: tracebacks truncated, skip receipts aggregated by reason.
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
    reasons = Counter(v.split(":")[0].split(" (")[0] for v in sel.get("selected", {}).values())
    return {
        "mode": sel["mode"],
        "n_total": sel["n_total"],
        "n_selected": len(sel["selected"]),
        "n_skipped": sel.get("n_skipped", 0),
        "changed_functions": sel.get("changed_functions", []),
        "run_all_reasons": sel.get("reasons", []),
        "selected_by_reason": dict(reasons),
        "tests": sorted(sel["selected"]),
        "skip_receipt": "every skipped test's recorded coverage is disjoint "
                        "from the changed functions; unmapped tests were selected",
    }


def run_via_daemon(node_ids: list[str]) -> dict | None:
    from fastest.daemon import SOCK, recv_msg, send_msg
    import socket

    if not os.path.exists(SOCK):
        return None
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(SOCK)
        send_msg(conn, {"op": "run", "node_ids": node_ids})
        resp = recv_msg(conn)
        conn.close()
        return resp
    except (ConnectionRefusedError, FileNotFoundError):
        return None


def run_via_subprocess(node_ids: list[str]) -> dict:
    from fastest.daemon import ResultCollector  # reuse the schema

    import pytest

    collector = ResultCollector()
    t0 = time.monotonic()
    stdout, sys.stdout = sys.stdout, open(os.devnull, "w")
    try:
        code = pytest.main(
            node_ids + ["-q", "--no-header", "-p", "no:cacheprovider"],
            plugins=[collector],
        )
    finally:
        sys.stdout.close()
        sys.stdout = stdout
    return {
        "exit": int(code),
        "wall_s": round(time.monotonic() - t0, 4),
        "results": collector.results,
    }


def cmd_run(args) -> dict:
    t0 = time.monotonic()
    aff = cmd_affected(args)
    if "error" in aff:
        return aff
    out = {
        "selection": {
            k: aff[k]
            for k in ("mode", "n_total", "n_selected", "n_skipped", "changed_functions",
                      "run_all_reasons")
        },
        "skip_receipt": aff["skip_receipt"],
    }
    if not aff["tests"]:
        out["results"] = []
        out["summary"] = {"status": "nothing_to_run", "wall_s": round(time.monotonic() - t0, 3)}
        return out

    resp = run_via_daemon(aff["tests"])
    out["executor"] = "daemon" if resp else "subprocess"
    if resp and (resp.get("stale") or resp.get("exit") in (2, 3, 4)):
        # warm image unusable (stale beyond re-import, or infra error):
        # run cold — slower, never wrong. A file-watching daemon that
        # re-warms on save is the product fix.
        out["executor"] = "subprocess (warm image stale — restart daemon)"
        out["daemon_stale_errors"] = resp.get("errors") or resp.get("pytest_output", "")[-500:]
        resp = None
    if resp is None:
        resp = run_via_subprocess(aff["tests"])
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
    out["summary"] = {
        "status": "failed" if failures else "passed",
        "passed": sum(1 for r in resp.get("results", []) if r["status"] == "passed"),
        "failed": len(failures),
        "ran": len(resp.get("results", [])),
        "skipped_by_selection": aff["n_skipped"],
        "exec_wall_s": resp.get("wall_s"),
        "total_wall_s": round(time.monotonic() - t0, 3),
    }
    return out


def main():
    import argparse

    ap = argparse.ArgumentParser(prog="fastest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("affected", "run"):
        p = sub.add_parser(name)
        p.add_argument("--base", default="HEAD")
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
