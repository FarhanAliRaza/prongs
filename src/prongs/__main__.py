"""Agent-facing CLI.

  python -m prongs affected [--base REV]             what would run, and why — JSON
  python -m prongs run [--base REV] [--no-cov]       select + execute + JSON results
                        [--no-record]
  python -m prongs audit [--base REV]                select, then run everything and
                                                      report every status change the
                                                      selection skipped, with receipts
  python -m prongs rollup                            fold pending journal files into
                                                      the map
  python -m prongs ci save DIR                       write the map and this job's
                                                      journal files as a CI artifact
  python -m prongs ci restore PATH...                install CI artifacts into a
                                                      fresh clone (see ci.py)
  python -m prongs daemon                            start the warm daemon
  python -m prongs stop                              stop the daemon

Run from the repo root (same directory you'd run pytest from).
`run` uses the warm daemon when its socket exists, else falls back to
spawning pytest. Output is a single JSON document on stdout, built for a
token budget: tracebacks truncated, failures grouped by exception line, skip
receipts aggregated by reason.

Every run appends one journal file and nothing but `rollup` writes the map;
affected, run and audit roll pending files up before they select (--no-rollup
to skip), so a run counts as soon as the next command reads the map. A run
records coverage unless --no-cov, which still appends its outcomes and
durations; --no-record appends nothing.

Every result carries `evidence`: which runs and commits the map comes from,
how far behind HEAD it is, the trees the diff was taken against (the
evidence's own, unless --base is given), and how much of the journal is
still pending. Every run result carries a `conservation` block — collected,
selected, run_all, skipped and executed counts, and whether they balance —
and a status that is 'error' when pytest exits 2-5 or a module fails to
collect, 'inconsistent' when the counts do not balance, never 'passed' for a
run that silently did less than asked; `recorded` and `record_ok` show that
the journal saw exactly what ran. A failing test that is flaky (both
outcomes on one tree within the last --flaky-window rollups) is re-run alone
up to --flaky-retries times: a pass reports it under `flaky`, failing every
time counts it as a failure.

Exit codes, for CI gates (the JSON document carries the detail):

  0  run: passed or nothing to run; audit: no miss; others: done
  1  run: tests failed; audit: a first-order miss
  2  error: pytest could not do what was asked (exit 2-5, a collection
     error, a crash), an unknown revision, no map to audit
  3  run: inconsistent — the conservation books do not balance
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

from prongs import config, journal, provenance
from prongs.select import select


def roll_up(repo: Path) -> dict:
    """Fold pending journal files into the map: lazily, before every
    selection, so a run's evidence counts as soon as the next command reads
    the map. The selector itself only reads."""
    from prongs import mapdb

    return mapdb.rollup(journal.map_path(repo), journal.journal_dir(repo))


def import_check(repo: Path, modules: list[str]) -> dict:
    """Collect every test module once, running nothing: a binding the
    selector judged unobserved (a new function, constant or import) still
    executes when its module is imported, and a selection that imports
    nothing would never find out that it raises."""
    from prongs import state

    t0 = time.monotonic()
    out = state.collect(repo, modules)
    out.pop("snapshot")
    return out | {"wall_s": round(time.monotonic() - t0, 3)}


def import_state_selection(repo: Path, aff: dict) -> tuple[set[str], dict] | None:
    """What the import-state rule (state.py) would select for a run that
    executes everything only because import-time code changed: the tests the
    remaining rules select, plus those that can reach what import now
    produces differently. None when the rule has nothing to go on."""
    from prongs import mapdb, state

    if config.import_state(repo) != "shadow" or not aff.get("run_all_reasons") or not all(
            r.startswith(state.REASONS) for r in aff["run_all_reasons"]):
        return None
    import sqlite3

    con = sqlite3.connect(journal.map_path(repo))
    try:
        base = None  # the newest recorded run that stored what import produced
        for run in reversed(mapdb.runs(con, lineage="own")):
            base = state.load(repo, run["journal_key"])
            if base is not None:
                break
        if base is None:
            return None
        modules = sorted({t.split("::", 1)[0] for t in aff["tests"]})
        now = state.collect(repo, modules)["snapshot"]
        if now is None:
            return None
        change = state.diff(base, now)
        tests = state.reach(con, repo, change) & set(aff["tests"])
    finally:
        con.close()
    info = state.summary(change)
    return set(aff["shadow"]) | tests, info


def shadow_baseline(repo: Path, aff: dict) -> dict[str, str] | None:
    """The map's statuses, read before a run of everything executes: what
    its results are compared with to audit the rules that would have skipped
    tests (see shadow_report)."""
    if aff.get("mode") != "run_all" or aff.get("shadow") is None:
        return None
    import sqlite3

    from prongs.audit import map_statuses

    con = sqlite3.connect(journal.map_path(repo))
    try:
        return map_statuses(con)
    finally:
        con.close()


def shadow_report(repo: Path, aff: dict, baseline: dict[str, str], results: list[dict],
                  rules: dict[str, set[str]]) -> dict:
    """A run of everything, read as an audit of rules that would have
    skipped tests: `selection` (the rules below the run-all one that fired)
    and, when it applies, `import_state`. For each: what it would have
    selected, and the skipped tests whose outcome differs from the map's. A
    difference is a miss of the rule, or a flake, or fallout from a selected
    failure: a log to review, not a verdict. One line per run is appended to
    <state dir>/shadow.jsonl; `prongs shadow` summarizes them."""
    changed = sorted(
        r["id"] for r in results
        if r["status"] in ("passed", "failed") and baseline.get(r["id"]) in ("passed", "failed")
        and baseline[r["id"]] != r["status"]
    )
    report: dict = {"reasons": aff["run_all_reasons"], "status_changes": len(changed), "rules": {}}
    for name, selected in rules.items():
        missed = [t for t in changed if t not in selected]
        report["rules"][name] = {
            "would_select": len(selected),
            "would_skip": aff["n_total"] - len(selected),
            "missed": missed[:50],
            "n_missed": len(missed),
        }
    line = report | {"at": round(time.time(), 1), "head": provenance.git_head(repo)}
    try:
        with open(journal.state_dir(repo) / "shadow.jsonl", "a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError:
        pass
    return report


def cmd_shadow(args) -> dict:
    """Every recorded run of everything, as audits of the rules a run-all
    rule overrode: how often each reason fired, what selecting would have
    saved, and which outcomes it would have missed."""
    path = journal.state_dir(Path.cwd()) / "shadow.jsonl"
    runs = []
    if path.exists():
        for ln in path.read_text().splitlines():
            try:
                runs.append(json.loads(ln))
            except ValueError:
                continue
    by_rule: dict[str, dict] = {}
    for r in runs:
        for rule, got in r.get("rules", {}).items():
            for reason in r["reasons"]:
                kind = reason.split(":")[0]
                b = by_rule.setdefault(rule, {}).setdefault(kind, {
                    "runs": 0, "would_skip": 0, "status_changes": 0, "n_missed": 0, "missed": []})
                b["runs"] += 1
                b["would_skip"] += got["would_skip"]
                b["status_changes"] += r["status_changes"]
                b["n_missed"] += got["n_missed"]
                b["missed"] = sorted(set(b["missed"]) | set(got["missed"]))[:50]
    return {"runs": len(runs),
            "clean": sum(all(g["n_missed"] == 0 for g in r.get("rules", {}).values()) for r in runs),
            "by_rule": by_rule}


def cmd_affected(args, force_all: str | None = None) -> dict:
    repo = Path.cwd()
    db, jdir = journal.map_path(repo), journal.journal_dir(repo)
    rolled = roll_up(repo) if getattr(args, "rollup", True) else None
    if not db.exists():
        return no_map_selection(len(journal.pending_all(jdir)))
    cfg = settings(repo, args)
    sel = select(db, repo, args.base, history_window=cfg["history_window"],
                 max_map_age=cfg["max_map_age"], flaky_window=cfg["flaky_window"],
                 force_all=force_all)
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
        "broadened": sel.get("broadened", []),
        "units": sel.get("units", {}),
        "import_check": sel.get("import_check", []),
        "shadow": sel.get("shadow"),
        "import_state": sel.get("import_state"),
        # flaky tests still run; their history is the receipt, and a failure
        # of one is reported as flaky, not as a failure
        "flaky": {
            t: f for t, f in sel["flaky"].items()
            if t in sel["selected"] or t.split("::", 1)[0] in sel.get("selected_files", {})
        },
        "history": sel["history"],
        "targets": sel["targets"],
        "deselect": sel.get("deselect", []),
        "evidence": sel["evidence"] | {"journal": {
            "rolled_up_now": rolled["rolled_up"] if rolled else 0,
            "pending": len(journal.pending_all(jdir)),
        }},
        "skip_receipt": skip_receipt(sel),
    }


def settings(repo: Path, args) -> dict:
    """[tool.prongs], environment, then this command's flags."""
    return config.settings(repo, **{name: getattr(args, name, None) for name in config.DEFAULTS})


def no_map_selection(pending: int) -> dict:
    """No evidence at all (a first CI run, an expired cache): the only safe
    selection is everything, as pytest itself would collect it. A run
    records coverage by default, so it builds the map it lacked."""
    reason = "no coverage map" + (
        f" ({pending} journal file(s) pending: roll them up)" if pending
        else " (this run records one)"
    )
    return {
        "mode": "run_all",
        "whole_suite": True,  # no node ids: pytest's own collection is the target
        "n_total": None,
        "n_selected": None,
        "n_skipped": 0,
        "changed_functions": [],
        "run_all_reasons": [reason],
        "selected_by_reason": {},
        "tests": [],
        "selected_files": {},
        "vanished": [],
        "broadened": [],
        "flaky": {},
        "history": None,
        "targets": [],
        "deselect": [],
        "evidence": {"map": None, "journal": {"rolled_up_now": 0, "pending": pending}},
        "skip_receipt": {"skipped": 0, "rule": f"nothing skipped: {reason}"},
        "hint": "any recorded run builds the map: prongs run, or pytest --prongs-cov",
    }


def skip_receipt(sel: dict) -> dict:
    """The auditable basis for every skip: the rule, and how fresh the
    evidence behind it is — the map's age in commits behind HEAD and how
    many tests it has no dependencies for. Not a proof — a transparent
    conservative policy."""
    ev = sel["evidence"]
    age = ev["map_age"]
    freshness = {
        "map_age": {
            "commit": age["commit"],
            "commits_behind_head": age["commits_behind_head"],
            "max_map_age": age["max_map_age"],
        } | ({"unknown": age["unknown"]} if "unknown" in age else {}),
        "unmapped_tests": ev["unmapped_tests"],
        "broadened": sel.get("broadened", []),
        # what the unit analysis decided: bindings added or removed with
        # nothing to observe them, and changed functions no test enters
        "additive": sel.get("units", {}).get("additive", {}),
        "unexecuted": sel.get("units", {}).get("unexecuted", []),
    }
    if sel["mode"] == "run_all":
        return {"skipped": 0, "rule": "nothing skipped: " + "; ".join(sel["reasons"])} | freshness
    return {
        "skipped": sel.get("n_skipped", 0),
        "rule": "a test is skipped only when, relative to the tree of the run that last "
                "observed it, no changed function or file is in its recorded dependency "
                "set; unmapped, new and statically-unfound tests, tests whose outcome "
                "flipped recently, and every test touching a file with a changed function "
                "the map has never seen always run",
        **freshness,
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


# Python's own traceback for each failure, not pytest's long or short style:
# those parse the source file of every frame again for every failure, which
# is most of a run that breaks many tests (httpx, 685 failures: 38.9s with
# pytest's default, 7.0s native). The crash line failures are grouped by,
# and pytest's assertion explanations, are in both.
TRACEBACKS = "--tb=native"


def run_via_daemon(targets: list[str], extra_args: list[str]) -> dict | None:
    from prongs.daemon import recv_msg, send_msg, sock_path
    import socket

    SOCK = sock_path()
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
    from prongs.daemon import ResultCollector, execution_response  # the daemon's schema

    import io

    import pytest

    collector = ResultCollector()
    # the same extra arguments the daemon's children get (daemon.serve)
    run_args = shlex.split(os.environ.get("PRONGS_RUN_ARGS", ""))
    t0 = time.monotonic()
    buf = io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf  # stdout is the JSON document; nothing else may print
    crash = None
    try:
        code = pytest.main(
            run_args + targets + extra_args + ["-q", "--no-header", "-p", "no:cacheprovider"],
            plugins=[collector],
        )
    except (Exception, SystemExit) as e:
        # a plugin raised during pytest's own start-up (pytest-django's
        # django.setup() importing broken models, say): pytest.main never
        # returned an exit code, and nothing ran
        import traceback

        buf.write(traceback.format_exc())
        crash, code = e, 3
    finally:
        sys.stdout, sys.stderr = saved
    # exit 2-5 is an error result, exactly as from the daemon: a collection
    # failure that ran nothing must never read as "passed"
    resp = execution_response(code, time.monotonic() - t0, collector, buf.getvalue)
    if crash is not None:
        # before any test (a plugin's start-up) or after them (an exception
        # out of unconfigure: under filterwarnings=error, the leaked resources
        # of a broken close() arrive as an ExceptionGroup at the very end)
        when = (f"after {len(collector.results)} test(s) ran" if collector.results
                else "before running tests")
        resp["error"] = f"pytest crashed {when}: {type(crash).__name__}: {crash}"
    return resp


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
    ids = [r["id"] for r in results]
    if aff.get("whole_suite"):
        # no map to balance against: the run's own collection is the census,
        # and a module that failed to collect makes the run an error
        return {"collected": None, "selected": 0, "run_all": len(set(ids)), "skipped": 0,
                "executed": len(ids), "conserved": len(set(ids)) == len(ids),
                "census": "no map: the run's own collection"}
    planned = set(aff["tests"])
    run_all = len(planned) if aff["mode"] == "run_all" else 0
    block = {
        "collected": aff["n_total"],
        "selected": len(planned) - run_all,
        "run_all": run_all,
        "skipped": aff["n_skipped"],
        "executed": len(results),
    }
    ran = set(ids)
    vanished = set(aff.get("vanished", []))
    # a parametrization the map knows and the function no longer has (an id
    # built from a timestamp or a pid, a renamed parameter): the function
    # ran, under the ids it has now
    ran_functions = {i.split("[", 1)[0] for i in ran}
    renamed = {t for t in planned - ran - vanished
               if "[" in t and t.split("[", 1)[0] in ran_functions}
    not_executed = sorted(planned - ran - vanished - renamed)
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
    if renamed:
        block["renamed"] = len(renamed)
    if ran - planned:
        block["unplanned"] = len(ran - planned)
    return block


def recorded_run(repo: Path, key: str) -> dict | None:
    """The journal file a run wrote under `key`, pending or already rolled up."""
    path = journal.find(journal.journal_dir(repo), key)
    return journal.read_run(path) if path else None


def append_results(repo: Path, jdir: Path, key: str, targets: list[str], results: list[dict],
                   resp: dict, snap: dict, started: float) -> None:
    """A run without coverage still appends: outcomes and durations, no
    dependency evidence, with the same provenance a recorded run carries."""
    journal.append(
        jdir, str(repo) + os.sep,
        {r["id"]: (r["duration_s"], r["status"], None) for r in results},
        {
            "started_at": started, "finished_at": time.time(), "scope": "partial",
            "mode": "results", "worktree": str(repo),
            **provenance.between(snap, provenance.snapshot(repo)),
            "recorder": provenance.recorder_fingerprint(), "args": targets,
            "n_collected": None,
            "collect_errors": {
                e["id"]: (e["longrepr"].strip().splitlines() or ["collection error"])[-1][:300]
                for e in resp.get("collect_errors", [])
            },
            "collect_skipped": resp.get("collect_skipped", []),
        },
        key=key,
    )


def flaky_results(repo: Path, aff: dict, results: list[dict], tree: dict,
                  window: int) -> dict[str, dict]:
    """test_id -> entry for every flaky test that ran: known flaky from
    history (passed and failed on one tree within the flaky window), or
    failing now on a tree where it passed before. Reported under `flaky`
    with its history; a failing one is retried (retry_flaky) before it is
    counted either way."""
    from prongs import mapdb
    from prongs.select import contradicted_on_tree

    known = aff.get("flaky", {})
    failed = [r["id"] for r in results if r["status"] == "failed" and r["id"] not in known]
    caught: dict[str, dict] = {}
    if failed and journal.map_path(repo).exists():  # no map: no history to contradict
        con = mapdb.connect(str(journal.map_path(repo)))
        try:
            caught = contradicted_on_tree(con, failed, tree, provenance.recorder_fingerprint(),
                                          window)
        finally:
            con.close()
    out = {}
    for r in results:
        f = known.get(r["id"]) or caught.get(r["id"])
        if f:
            out[r["id"]] = {
                "test": r["id"], "status": r["status"],
                "why": f"passed {f['passed']}x and failed {f['failed']}x on tree {f['tree']}",
                "history": f["history"],
            }
    return out


def retry_flaky(repo: Path, flaky: dict[str, dict], retries: int, record: bool,
                warm: bool) -> None:
    """Re-run each failing flaky test alone, up to `retries` times, until it
    passes. A pass makes its failure a flake; failing every time makes it a
    failure, flaky or not — a known flake is no licence to fail. Retries are
    recorded like any run (unless --no-record), so a pass lands in history
    next to the failure on the same tree: the next run knows the flake.
    Retries use the warm daemon when the run did, else a fresh pytest."""
    from prongs.audit import observe

    jdir = journal.journal_dir(repo)
    for entry in flaky.values():
        if entry["status"] != "failed":
            continue
        entry["retries"] = []
        for _ in range(retries):
            status = None
            if warm:
                key = journal.new_key()
                extra = ["--tb=no"] + (["--prongs-cov", "--prongs-journal", str(jdir),
                                        "--prongs-journal-key", key] if record else [])
                resp = run_via_daemon([entry["test"]], extra)
                if resp and not resp.get("stale") and "error" not in resp:
                    status = next((r["status"] for r in resp.get("results", [])
                                   if r["id"] == entry["test"]), "absent")
            if status is None:
                args = shlex.split(os.environ.get("PRONGS_RUN_ARGS", ""))
                status = observe(repo, sys.executable, args, targets=[entry["test"]],
                                 record=record)["statuses"].get(entry["test"], "absent")
            entry["retries"].append(status)
            if status == "passed":
                break
        entry["counted_as"] = "flaky" if "passed" in entry["retries"] else "failure"


def rerun_failures(repo: Path, failures: list[dict], record: bool) -> list[dict]:
    """The failures of a selected run that pass in a fresh process holding
    only the failures: order-dependent ones. A selected run is the suite
    with tests left out, so a test can meet state a test before it left
    behind and that, in the suite, a test in between tidies up. What the
    change broke fails here as it did there.

    With `record`, a re-run in which something passed is kept as a run of
    its own: the pass lands in history next to the failure, on one tree (the
    test is flaky from then on), and the map gets what the test touches when
    it passes. It stores no import-state snapshot: the run it follows has
    this tree's."""
    from prongs.audit import observe

    obs = observe(repo, sys.executable, shlex.split(os.environ.get("PRONGS_RUN_ARGS", "")),
                  targets=sorted(r["id"] for r in failures), record=record,
                  env={"PRONGS_IMPORT_STATE": "0"})
    passed = [r for r in failures if obs["statuses"].get(r["id"]) == "passed"]
    if not passed and obs["journal"]:
        path = journal.find(journal.journal_dir(repo), obs["journal"])
        if path:
            path.unlink()  # the same failures again: nothing the first run did not say
    return [
        {"test": r["id"], "error": exception_line(r), "alone": "passed",
         "why": "failed after the other selected tests, passed in a fresh process without them"}
        for r in passed
    ]


def cmd_run(args) -> dict:
    t0 = time.monotonic()
    repo = Path.cwd()
    aff = cmd_affected(args, force_all=getattr(args, "force_all", None))
    if "error" in aff:
        return aff
    checked = None
    if aff.get("import_check"):
        checked = import_check(repo, aff["import_check"])
        if not checked["ok"]:
            # the tree does not even import: run everything, so the errors
            # are reported as any collection error is
            args.rollup = False
            aff = cmd_affected(args, force_all="import check failed: collecting the suite "
                                               f"exited {checked['exit']}")
    out = {
        "selection": {
            k: aff[k]
            for k in ("mode", "n_total", "n_selected", "n_skipped", "changed_functions",
                      "run_all_reasons", "selected_files")
        },
        "evidence": aff["evidence"],
        "skip_receipt": aff["skip_receipt"],
    }
    if checked is not None:
        out["import_check"] = checked
    if aff.get("import_state"):
        out["import_state"] = aff["import_state"]
    baseline = shadow_baseline(repo, aff)
    shadow_rules: dict[str, set[str]] = {}
    if baseline is not None:
        shadow_rules["selection"] = set(aff["shadow"])
        by_state = import_state_selection(repo, aff)
        if by_state is not None:
            shadow_rules["import_state"], out["import_state"] = by_state
    if not aff["targets"] and not aff.get("whole_suite"):
        out["results"] = []
        out["conservation"] = conservation(aff, [])
        out["summary"] = {
            "status": "nothing_to_run" if out["conservation"]["conserved"] else "inconsistent",
            "wall_s": round(time.monotonic() - t0, 3),
        }
        return out

    # every run appends to the journal (unless --no-record): with coverage the
    # recorder writes the file; without, this process writes the outcomes
    mode = ("coverage" if args.cov else "results") if args.record else None
    key = journal.new_key() if mode else None
    jdir = journal.journal_dir(repo)
    extra = [TRACEBACKS] + [f"--deselect={t}" for t in aff.get("deselect", [])] + (
        ["--prongs-cov", "--prongs-journal", str(jdir), "--prongs-journal-key", key]
        if mode == "coverage" else []
    )
    snap, started = provenance.snapshot(repo), time.time()  # the tree this run executes
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
    if mode == "results":
        append_results(repo, jdir, key, aff["targets"], results, resp, snap, started)
    cfg = settings(repo, args)
    flaky = flaky_results(repo, aff, results, snap, cfg["flaky_window"])
    t_retry = time.monotonic()
    retry_flaky(repo, flaky, cfg["flaky_retries"], record=mode is not None,
                warm=out["executor"] == "daemon")
    failures = [r for r in results if r["status"] == "failed"
                and flaky.get(r["id"], {}).get("counted_as") != "flaky"]
    order_dependent: list[dict] = []
    # a flaky test has its retries, and its own verdict
    suspects = [r for r in failures if r["id"] not in flaky]
    if suspects and aff["mode"] == "select" and cfg["isolate_failures"]:
        # kept as a run only when its pass is the last word: before a run
        # of the whole suite, a recorded pass would make the test flaky and
        # the suite's failure of it a flake
        order_dependent = rerun_failures(
            repo, suspects, record=mode == "coverage" and cfg["isolate_failures"] == 1)
        excused = {e["test"] for e in order_dependent}
        failures = [r for r in failures if r["id"] not in excused]
    retry_s = time.monotonic() - t_retry
    out["failures"] = group_failures(failures)  # passes are summarized, not listed
    if flaky:
        out["flaky"] = list(flaky.values())
    if order_dependent and cfg["isolate_failures"] >= 2:
        # passing alone proves nothing about the suite: a change can make one
        # test break another. The whole suite is what the selected run stands
        # in for, so it settles them, and its verdict is this run's
        # (in a process of its own: this one holds the selected run's
        # modules, and whatever its tests left in them)
        reason = (f"{len(order_dependent)} failure(s) of the selected run passed without "
                  "the other selected tests: the whole suite settles them")
        p = subprocess.run([sys.executable, "-m", "prongs", *sys.argv[1:], "--force-all", reason],
                           capture_output=True, text=True, cwd=repo)
        try:
            full = json.loads(p.stdout)
        except ValueError:
            full = {"error": "the whole-suite run produced no result",
                    "output": (p.stdout + p.stderr)[-2000:],
                    "summary": {"status": "error"}}
        still = {f["representative"] for f in full.get("failures", [])} | {
            t for f in full.get("failures", []) for t in f["also_failed"]}
        full["order_dependent"] = [
            e | {"suite": "failed" if e["test"] in still else "passed",
                 "why": "failed after the other selected tests, passed in a fresh process "
                        "without them; settled by the whole suite"}
            for e in order_dependent]
        full["selected_run"] = {"n_selected": aff["n_selected"], "ran": len(results),
                                "wall_s": round(time.monotonic() - t0, 3)}
        if "summary" in full:
            full["summary"]["order_dependent"] = len(order_dependent)
            full["summary"]["total_wall_s"] = round(
                full["summary"].get("total_wall_s", 0) + full["selected_run"]["wall_s"], 3)
        return full
    if order_dependent:
        out["order_dependent"] = order_dependent
    ran = len(results)
    out["conservation"] = conservation(aff, results)
    if baseline is not None:
        out["shadow"] = shadow_report(repo, aff, baseline, results, shadow_rules)
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

    ran_functions = {i.split("[", 1)[0] for i in ran_ids}
    ran_files = {i.split("::", 1)[0] for i in ran_ids}

    def produced_results(target: str) -> bool:
        # a function target: any of its parametrizations; a whole-file target: any test
        return target in ran_functions if "::" in target else target in ran_files

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
        "flaky": {
            "ran": len(flaky),
            "failed": sum(f["status"] == "failed" for f in flaky.values()),
            "passed_on_retry": sum(f.get("counted_as") == "flaky" for f in flaky.values()),
            "retries": cfg["flaky_retries"],
        },
        "order_dependent": len(order_dependent),
        "ran": ran,
        "unrun_targets": unrun,
        "complete": not unrun and not error,
        "skipped_by_selection": aff["n_skipped"],
        "exec_wall_s": resp.get("wall_s"),
        "retry_wall_s": round(retry_s, 3),
        "total_wall_s": round(time.monotonic() - t0, 3),
    }
    if "pytest_output" in resp:
        out["pytest_output"] = resp["pytest_output"]
    if key:
        # in == out, part 2: the run we executed must be the run the journal saw
        rec = recorded_run(repo, key)
        out["summary"]["recorded"] = (
            {"journal": key, "mode": rec["mode"], "scope": rec["scope"],
             "n_observed": rec["n_observed"]}
            if rec else None
        )
        out["summary"]["record_ok"] = bool(rec) and rec["n_observed"] == ran
    return out


def cmd_audit(args) -> dict:
    from prongs.audit import audit, checkout_control

    repo = Path.cwd()
    if args.rollup:
        roll_up(repo)
    if not journal.map_path(repo).exists():
        return {"error": "no coverage map",
                "hint": "build one with: pytest --prongs-cov (then prongs rollup)"}
    raw = args.pytest_args if args.pytest_args is not None else os.environ.get("PRONGS_RUN_ARGS", "")
    control = checkout_control(repo, pytest_args=shlex.split(raw)) if args.control else None
    res = audit(repo, args.base, pytest_args=shlex.split(raw), isolate=args.isolate,
                record=args.record, rollup=False, history_window=args.history_window,
                max_map_age=args.max_map_age, flaky_window=args.flaky_window,
                control=control)
    res.pop("statuses", None)  # per-test statuses are for harnesses, not the agent
    return res


def cmd_ci(args) -> dict:
    from prongs import ci

    repo = Path.cwd()
    if args.ci_cmd == "save":
        return ci.save(repo, Path(args.dir), with_map=args.map, with_journals=args.journals)
    return ci.restore(repo, args.paths, fetch=args.fetch, remote=args.remote)


def cmd_rollup(args) -> dict:
    repo = Path.cwd()
    res = roll_up(repo)
    res["map"] = str(journal.map_path(repo))
    res["pending"] = len(journal.pending_all(journal.journal_dir(repo)))
    return res


EXIT_OK, EXIT_FAILED, EXIT_ERROR, EXIT_INCONSISTENT = 0, 1, 2, 3
RUN_EXIT = {"passed": EXIT_OK, "nothing_to_run": EXIT_OK, "failed": EXIT_FAILED,
            "error": EXIT_ERROR, "inconsistent": EXIT_INCONSISTENT}


def headline(out: dict) -> str:
    """A run in one line, for whoever reads the log (stdout is the JSON
    document, for programs): how it ended, what ran of how much, and why
    everything ran when it did."""
    summary, sel = out.get("summary"), out.get("selection")
    if not summary or not sel:
        return f"prongs: {out.get('error', 'no result')}"
    known = "" if sel["n_total"] is None else f" of {sel['n_total']}"  # None: no map yet
    line = f"prongs: {summary['status']}: ran {summary.get('ran', 0)}{known} tests"
    if sel["mode"] == "run_all":
        line += f" (everything: {'; '.join(sel['run_all_reasons'])})"
    else:
        line += f" ({sel['n_skipped']} not affected by the change)"
    if summary.get("failed"):
        line += f", {summary['failed']} failed"
    return line + f" in {summary.get('total_wall_s', summary.get('wall_s', 0)):.0f}s"


def exit_code(cmd: str, out: dict) -> int:
    """The process exit code for a command's JSON result: what a CI step
    gates on."""
    if cmd == "run" and "summary" in out:
        return RUN_EXIT[out["summary"]["status"]]
    if cmd == "audit" and "verdict" in out:
        return {"pass": EXIT_OK, "miss": EXIT_FAILED}.get(out["verdict"], EXIT_ERROR)
    return EXIT_ERROR if "error" in out else EXIT_OK


def main():
    import argparse

    ap = argparse.ArgumentParser(prog="prongs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("affected", "run", "audit"):
        p = sub.add_parser(name)
        p.add_argument("--base", default=None,
                       help="diff base (default: the commits the evidence was observed at)")
        p.add_argument("--rollup", action=argparse.BooleanOptionalAction, default=True,
                       help="fold pending journal files into the map first (default: on)")
        p.add_argument("--history-window", type=int, default=None, metavar="N",
                       help="select tests whose outcome flipped in the last N rollups "
                            "(default: [tool.prongs] history_window, else 3)")
        p.add_argument("--max-map-age", type=int, default=None, metavar="N",
                       help="run everything when the map is more than N commits behind HEAD "
                            "(default: [tool.prongs] max_map_age, else 50)")
        p.add_argument("--flaky-window", type=int, default=None, metavar="N",
                       help="flaky evidence (both outcomes on one tree) older than N rollups "
                            "expires (default: [tool.prongs] flaky_window, else 50)")
        if name == "run":
            p.add_argument("--record", action=argparse.BooleanOptionalAction, default=True,
                           help="append this run to the journal, refreshing the tests it "
                                "runs (default: on)")
            p.add_argument("--cov", action=argparse.BooleanOptionalAction, default=True,
                           help="record per-test coverage; --no-cov still appends outcomes "
                                "and durations (default: on)")
            p.add_argument("--flaky-retries", type=int, default=None, metavar="N",
                           help="re-run a failing flaky test alone up to N times; a pass makes "
                                "it a flake, else it is a failure (default: [tool.prongs] "
                                "flaky_retries, else 2; 0: a flaky failure is a failure)")
            p.add_argument("--force-all", default=None, help=argparse.SUPPRESS)
            p.add_argument("--isolate-failures", type=int, default=None, metavar="0|1|2",
                           help="re-run the failures of a selected run in a fresh process "
                                "without the other selected tests; one that passes there is "
                                "order-dependent, not a failure (default: [tool.prongs] "
                                "isolate_failures, else 2: the whole suite then settles "
                                "it; 1: it is reported and not a failure; 0: it is a failure)")
        if name == "audit":
            p.add_argument("--pytest-args", default=None,
                           help="extra pytest arguments for the full run "
                                "(default: $PRONGS_RUN_ARGS)")
            p.add_argument("--isolate", action=argparse.BooleanOptionalAction, default=True,
                           help="re-run each miss alone to separate first-order misses "
                                "from second-order pollution (default: on)")
            p.add_argument("--record", action=argparse.BooleanOptionalAction, default=False,
                           help="append the full run to the journal instead of a temporary "
                                "database (default: off — an audit never feeds the map)")
            p.add_argument("--control", action=argparse.BooleanOptionalAction, default=False,
                           help="re-run each first-order miss alone on the commit its baseline "
                                "was observed on, checked out in place (clean working tree "
                                "only), to tell environment drift from a miss (default: off)")
    sub.add_parser("rollup")
    sub.add_parser("shadow")
    ci = sub.add_parser("ci", help="carry the map and journal files between CI jobs")
    ci_sub = ci.add_subparsers(dest="ci_cmd", required=True)
    p = ci_sub.add_parser("save", help="write an artifact directory")
    p.add_argument("dir")
    p.add_argument("--map", action=argparse.BooleanOptionalAction, default=True,
                   help="a snapshot of the map, pending journal files rolled up first "
                        "(default: on; a pull-request job needs --no-map)")
    p.add_argument("--journals", action=argparse.BooleanOptionalAction, default=True,
                   help="the journal files this checkout wrote (default: on)")
    p = ci_sub.add_parser("restore", help="install artifact directories into this checkout")
    p.add_argument("paths", nargs="+", help="artifact directories, or directories holding them")
    p.add_argument("--fetch", action=argparse.BooleanOptionalAction, default=True,
                   help="deepen a shallow clone until the map's evidence commits are present "
                        "(default: on)")
    p.add_argument("--remote", default="origin", help="the remote to fetch history from")
    sub.add_parser("daemon")
    sub.add_parser("stop")
    args = ap.parse_args()

    if args.cmd == "daemon":
        from prongs.daemon import serve

        serve()
    elif args.cmd == "stop":
        from prongs.daemon import recv_msg, send_msg, sock_path
        import socket

        sock = sock_path()
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(sock)
            send_msg(c, {"op": "stop"})
            recv_msg(c)
            c.close()
            out = {"stopped": True}
        except (FileNotFoundError, ConnectionRefusedError):
            # no daemon; a socket file left by one that died is cleared
            if os.path.exists(sock):
                os.unlink(sock)
            out = {"stopped": False, "reason": "no daemon running"}
        print(json.dumps(out, indent=2))
    else:
        commands = {"affected": cmd_affected, "run": cmd_run, "audit": cmd_audit,
                    "rollup": cmd_rollup, "ci": cmd_ci, "shadow": cmd_shadow}
        out = commands[args.cmd](args)
        print(json.dumps(out, indent=2))
        code = exit_code(args.cmd, out)
        if args.cmd == "run":
            print(headline(out), file=sys.stderr)
            # the run's files are closed and its JSON is out; tearing down the
            # in-process pytest session's objects is all that is left, and on
            # DRF (1,626 tests) that took 0.5s of a 9s run
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
        sys.exit(code)


if __name__ == "__main__":
    main()
