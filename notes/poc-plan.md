# PoC execution plan: prove it or kill it

Goal: validate the four load-bearing technical claims of [[hypothesis]] with the cheapest possible experiments, on **real repos**, with explicit kill criteria. Not a product — a series of measurements. Everything in Python first; Rust is an optimization detail, not a risk, so it proves nothing at PoC stage.

Note on [[verdict]]'s build order: it puts a full differential-compatibility corpus first (its Milestone 1). That's the right first milestone *for a product already greenlit*, wrong for a prove-or-kill PoC — a beautiful compat oracle validates nothing if the map is expensive or replay shows misses. We keep risk ordering (map → replay → daemon) and adopt the verdict's *content*: generation fingerprints in Phase 3, its conservative-rules list as the Phase 2 spec, and a PoC-sized parity oracle inside Phase 4.

**The one demo the PoC must end with:** change one function in a repo with a multi-minute suite → get trustworthy verified results in under 2 seconds → show the receipt for every skipped test. If that demo works, everything else is engineering.

## Test beds (pick 3, clone once)

- **Small/clean:** `httpx` or `rich` (~1–3k tests, fast suite) — baseline sanity.
- **Django app:** `saleor` or `wagtail` — the DB-fixture / conftest-heavy worst case, and where the pain evidence came from.
- **Big/weird:** `pandas` or `home-assistant` (10k+ tests, heavy imports, dynamic collection edge cases) — stress test.

## Phase 0 — Benchmark harness (1 day)

A script that, per repo, records: cold `pytest --collect-only` time, cold full-run time, cold single-test time (`pytest path::test`), import time of the test tree (`-X importtime`), token count of failure output. This is the before-picture every later phase is measured against. No kill criterion — this is the ruler.

## Phase 1 — Per-test coverage map via `sys.monitoring` (2–4 days) — THE WEDGE

Build a minimal pytest plugin: on each test start, enable `sys.monitoring` line/call events; on test end, dump {test_id → set of (file, function)} into SQLite.

- Measure: overhead vs plain run, on all 3 repos. Map size on disk. Map build time.
- **Kill criterion: overhead > 25%** on the Django repo. (coverage.py's old settrace cost 2–5x; the hypothesis claims sys.monitoring makes this cheap — this is the claim's first contact with reality.)
- Also record: % of tests whose map is empty/unusable (C extensions, subprocesses) — these become the "always run" conservative set, and if that set is >40% of the suite, selection's multiplier collapses.

## Phase 2 — Selection with receipts, validated against history (3–5 days) — THE PRODUCT CLAIM

Given the Phase-1 map and a git diff: changed files/functions → inverted map → affected test set + conservative set. The conservative broadening rules (adopted from [[verdict]] as the spec):

- changed `conftest.py` → run its entire collection scope
- changed pytest config (`pyproject`/`pytest.ini`/`setup.cfg`) → recollect and broaden
- changed lockfile or plugin version → map is stale, full run (later: fresh generation)
- new test or unmapped test → always run
- changed production file with **no reverse edges** → package-level fallback, then repo
- test whose observation was incomplete (subprocess/thread activity we couldn't attribute) → always run
- changed data file → run tests observed opening it (if file-open tracking lands in Phase 1; else broaden)

Receipts are this policy made auditable, not a proof — every skip prints which rule cleared it and how fresh the evidence is.

The validation that matters — **replay real history**: take the last ~50 commits of the test-bed repo. For each commit N: build map at N-1, compute selected set for the diff, run the *full* suite at N, check whether any test that changed status (pass↔fail) was outside the selected set.

- Measure: miss rate (must-be-zero), average % of suite selected, receipt quality (can we print *why* each skipped test was safe?).
- **Kill criterion: any missed status-change that the conservative rules don't catch.** This is the testmon lesson — one wrong skip and trust is gone. A high selected-% (say, always 60%+) is a softer kill: the 10–30x multiplier claim dies even if correctness holds.

## Phase 3 — Warm daemon + fork (1 week) — THE LATENCY CLAIM

A long-lived Python process that imports the app + pytest machinery + runs session fixtures once, then `os.fork()`s per run request; child executes the selected tests **by delegating to real pytest** (`pytest.main([...])` with node IDs) and reports over a pipe.

The warm process is a **disposable generation, never authoritative state** (per [[verdict]]): identify it by a fingerprint — interpreter, installed dists, pytest + plugin set, config, conftest hierarchy, imported app-code hashes — and when a fingerprint input changes and can't be localized, kill the generation and re-warm; never reload modules in place. Fallback to a fresh pytest process always exists and is part of the design, not a failure mode. Fork is Linux-first and known-hostile to threads (Python 3.14 moved multiprocessing's default to `forkserver` for exactly this reason) — a warmed Django/asyncio process carries threads, event loops, sockets, pools.

- Measure: request→first-test-executing latency (claim: ms, vs 2–8s cold), on the Django repo specifically — fork with an open Postgres connection is the known landmine (test the documented fix: close/reopen connections post-fork). Also measure generation re-warm time — it's the real steady-state cost, since every app-source edit retires the generation.
- Baseline to beat: rpytest (same daemon + warm-worker architecture) publishes 1.9× on a 480-test *full-suite* run — that's the warm-only ceiling for suite throughput. Our claim is selection × warm on the single-change loop, so measure that loop, not suite throughput.
- **Kill criterion: fork-unsafe state on the Django app that can't be fixed with a small post-fork re-init hook list**, or warm-start latency worse than ~10x better than cold. Partial pass is acceptable: if CUDA/threading repos are unfixable, scope the daemon to web-app workloads and say so.
- Bonus experiment (cheap here): fork-per-test after session fixtures — what does the child actually inherit? CoW covers *in-process* state only (Python objects, in-memory caches, maybe in-memory SQLite); Postgres state is isolated by Django's own transaction-rollback / per-worker DB clone, **not** by fork. Verify teardown-by-exit is really free and connections re-init cleanly.

## Phase 4 — Static collection parity (2–3 days) — THE COMPAT CLAIM

Python `ast`-based collector (no Rust): find test functions/classes/parametrize statically, compare output against `pytest --collect-only` on all 3 repos. Framing rule from [[verdict]]: **static proposes, pytest disposes** — static output is a speculative index, canonical node IDs always come from real pytest. The parity diff is the mechanism, not just a metric: classify each module `STATIC_SAFE` / `CANONICAL_REQUIRED` / `UNKNOWN`, and only ever trust the fast path on modules with established parity and unchanged inputs. This per-module diff is also the PoC-sized version of the verdict's differential compatibility oracle — node IDs and outcomes compared on the 3 testbeds; the full corpus-as-product waits for a go decision.

- Measure: parity % (exact node-ID match), collection speed vs pytest's, and — critically — whether mismatches are *detectable* statically (dynamic decorators, `__init__` magic, custom collectors, `pytest_collection_modifyitems` in conftest) so those files can be classified `CANONICAL_REQUIRED`.
- **Kill criterion: none, honestly** — <95% parity just means the fallback triggers more; what would actually hurt is *undetectable* mismatches (silently missing tests). Any silent miss → this component ships fallback-first.

## Phase 5 — Agent-facing output + the demo (2–3 days)

JSON result schema (status, duration, failure with diff-aware truncated traceback, receipt for every skip). Measure token count vs raw pytest output on a 10-failure run. Then wire Phases 1–4 together behind one command and run the end-to-end demo — including once *from inside a Claude Code session* editing the test-bed repo, timing the full edit→verified loop.

## What we deliberately do NOT build in the PoC

Rust anything, the scheduler, content-addressed caching, remote cache, MCP server, plugin API. All downstream of the four risks above.

## Timeline and decision gate

~3–4 weeks solo. Order is by risk: Phases 1–2 (coverage + selection replay) are the first two weeks and the real go/no-go — if the map is cheap and history-replay shows zero misses with a small conservative set, the product claim is proven and the rest is latency engineering. Decision memo at the end: numbers per phase vs kill criteria, and a recommendation (build as OSS wedge / fold into Reflex as a feature / drop).
