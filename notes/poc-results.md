# PoC results — day 1 (2026-07-12)

Everything below is measured, not projected. Code: `src/fastest/` (plugin, mapdb, select, daemon, CLI), harnesses in `bench/`, raw numbers in `results/`.

## Scoreboard vs kill criteria

| Phase | Claim | Kill criterion | Result | Verdict |
|---|---|---|---|---|
| 1 coverage map | cheap via `sys.monitoring` | >25% overhead on Django repo | **httpx ~6%, DRF ~11–13%** (v2: classify-once + permanent DISABLE for non-project code; v1 with per-test `restart_events()` was 31% — the fix mattered) | **PASS** |
| 2 selection | zero misses, small selected set | any missed status-change | **history replay (40 httpx commits): 0 misses**, selection 3–41% (median 28%) when engaged. **Mutation testing (15+15 injected faults): 30/30 detected, 0 misses** after the `__collection__` fix | **PASS** (with findings below) |
| 3 daemon+fork | ms warm runs, real pytest compat | unfixable fork-unsafety, <10x | **DRF: 107ms round trip vs 440ms cold single test**; DB-touching tests pass in the fork; two Django shims needed (settings.configure guard, model re-registration); stale-module purge handles post-edit runs (0.45s incl. purge) | **PASS** (needs xdist/postgres trials) |
| 5 agent output | token-frugal, root-cause grouped | — | same bug: raw pytest 12,871 B vs fastest JSON 2,201 B (**5.8x**); 11 failures deduped to 1 traceback + member list | **PASS** |
| 4 static collection | — | — | not started | pending |

**The end demo (DRF, 1573 tests, full suite 3.8s):** introduce a bug → `python -m fastest run` → **0.51s total wall**: selected 26 tests (receipts for 1547 skips), warm fork detected+purged stale modules, 11 failures grouped under one root-cause traceback matching mutation ground truth exactly.

## Findings that will shape the design

1. **Import-time execution is a real miss class, now handled.** DRF test modules execute functions at import (decorators, class-level field declarations). First mutation round: 3 misses from this. Fix: record everything executed during collection as a `__collection__` pseudo-context; a diff touching it ⇒ run-all. Refinement path: per-module import attribution (static import graph) to avoid run-all.
2. **Cross-test state pollution is the remaining (bounded) miss class.** Mutating `GenericAPIView.get_serializer_class` broke 5 tests whose maps legitimately don't contain it — they pass in isolation; they fail in-suite because an earlier *selected* failing test skips its manual cleanup (`reload_module(filters)` at test end) and poisons module globals. The diff's direct effects are always caught; only second-order fallout from an already-reported failure can be missed. **Fork-per-test isolation eliminates this class entirely** — the agent-isolation feature and selection soundness are the same mechanism. Strong argument to make fork-per-test the default execution mode.
3. **The daemon's hard problem is invalidation, not forking.** First demo silently ran stale code (bug edited after warm-up → "all green"). PoC fix: at fork, if any project-module mtime changed, purge all project modules from `sys.modules` (third-party import weight stays warm) + Django registry shims. Costs ~0.3s on DRF. This — plus file-watching — is the real engineering core of the runtime, as the hypothesis predicted.
4. **Stdout parsing is untrustworthy even for tooling** — httpx's `addopts` suppress FAILED summary lines, which silently broke the first mutation harness (0 detected failures from 15 real ones). Machine-readable results must come from inside the run (plugin → SQLite/JSON). This is the product's own thesis, self-demonstrated.
5. **Selection precision is high**: mutation of `QueryParams.__bool__` flipped 299 tests, selection had picked 309; `get_limit` flipped 11, selection picked 26.

## Wagtail (the heavy-repo stress test, added same day)

6286 tests, tests written for Django's own runner (`runtests.py`), run under pytest-django. The numbers that matter:

- **Cold single test: 32.7s** — ~31s of it is creating the test DB (wagtail's migrations also *seed* required data: root page, default site, locale — `--no-migrations` breaks 2369 tests, so real migrations are mandatory). This is the "run one test, wait half a minute" pain from the field research, reproduced exactly.
- **Warm daemon: 0.55–0.62s round trip, tests pass — 53x.** Warm-up pays imports + migrations once (36s), then every fork inherits the populated in-memory SQLite DB via copy-on-write.
- **Fixture-snapshot mechanics that made it work:** (a) warm phase runs one DB test to force pytest-django to create+migrate the DB in the parent; `--reuse-db` so session teardown doesn't destroy it; a pinned open connection so the shared-cache in-memory DB stays alive. (b) children patch `setup_databases` to a no-op and attach to their forked copy. (c) `TransactionTestCase` (truncates all tables in teardown) passes in a fork and the *next* fork still sees pristine data — destructive DB tests get free isolation.
- Remaining child overhead is pytest session init (~0.4s floor measured with a no-DB `SimpleTestCase`); next lever is forking from a post-collection template process instead of re-running `pytest.main` per child.
- **Coverage overhead on the big repo: 1.1%** (plain 331.1s vs instrumented 334.8s, 6231 passing) — overhead amortizes as test bodies grow; the 25% kill criterion is nowhere in sight. Map: 99.5% of tests mapped, 14.5k functions, 921k links, 24MB SQLite, ~13% of functions execute at import/collection time (the run-all set).
- **Selection on a real edit:** changing `Page.route` selects 220 of 6286 tests.
- **The invalidation frontier, mapped precisely (most valuable wagtail finding):** after an edit, the child's "purge project modules + re-import" strategy hit, in order: (1) circular imports — fixed by replaying the warm-up's `sys.modules` insertion order; (2) third-party import-time registries — django-taggit refuses a model class re-registering with the same through-model. Registries like this (taggit, admin, signals) make in-child re-import an open-ended adapter chase on complex Django apps. Resolution implemented: the child *refuses* to run on a stale-beyond-repair image (`StaleWarmImage`), and the CLI falls back to a cold subprocess run — slower, never wrong. The product fix is a file-watcher that re-warms the daemon in the background on save (36s, paid while the agent is thinking), so warm forks always come from a fresh image. Note: the same purge worked fine on DRF — the adapter tail scales with app complexity.

## Environment notes
- Test beds: httpx (1418 tests, py3.14), django-rest-framework (1573 tests, py3.12, real Django+DB via pytest-django/SQLite), rich (981 tests, daemon smoke test). DRF replaced saleor/wagtail for the Django case — no Postgres needed at PoC stage.
- All Python, no Rust, as planned.

## Next (in risk order)
1. Mutation + replay on a genuinely heavy repo (wagtail or saleor w/ Postgres; import weight makes the daemon ratio dramatic).
2. Fork-per-test execution mode (kills the pollution miss class; measure per-test fork cost).
3. Phase 4 static collection parity.
4. `affected`/`run` behind MCP or direct CLI in a real agent session end-to-end.
5. Shared journal across worktrees (each worktree currently pays its own full instrumented run) — see the 2026-09-24 section below.

# Journal + provenance — 2026-09-24

The "adopt now" list from [[anthropic-tia-scaling]] implemented: append-only journal + derived roll-up, provenance (HEAD and dirty files with content) on every run, evidence in every receipt, selected runs feed the map. Code: `mapdb.py`, `provenance.py`, `plugin.py`, `select.py`, `__main__.py`; 38 tests in `tests/` driving the real recorder in a temp git repo (pass on 3.12 and 3.13); harness numbers in `results/`. Everything below is measured.

## Scoreboard (httpx @ b5addb6, 1418 tests, py3.12, pytest 8.4.1)

| Claim | Kill criterion | Result | Verdict |
|---|---|---|---|
| journal write path keeps the recorder cheap | >25% overhead | plain 14.14s vs instrumented 14.75s: **4.4%** (July: ~6%) | **PASS** |
| per-tree selector never misses on real history | any miss | 8 commits / 7 pairs: **0 misses**; selected 0.1% median, 7.5% and 21.1% on the two larger commits, never run-all | **PASS** |
| mutation testing | any first-order miss | 12 sampled mutations, 11 caused failures, **0 misses, 0 pollution**; 2 targeted teardown-path mutations (`Client.__exit__`, `AsyncClient.aclose`): **0 first-order misses, 7 second-order pollution** — every one passes alone under the mutation, and identical suite runs disagree on which tests are hit | **PASS** (finding 1) |
| evidence is cheap | — | `affected` on a clean tree: **0.13s** wall (34k dependency links, 572 content-addressed sets, 1.2MB map) | — |

## The agent loop, measured (httpx)

edit `URL.host` → `affected` selects 516/1418 → `run` records them (12.9s; run #2, partial, `record_ok`) → `affected` → **1** (the one unmapped test) → edit `Headers.get` → **464** selected: the 516 already verified against `URL.host` are diffed only against the content run #2 saw, so only their `Headers.get` dependents come back, while tests last seen by the full run are diffed against both edits → `run` (8.4s) → `affected` → 1 → commit both → still 1, `commits_behind=1`. With the map frozen at the full run (the July design, or `--no-record` now) the third `affected` re-selects all 516 plus every `Headers.get` dependent.

## Findings

1. **The mutation kill criterion was conflating two classes.** First sample: 8 "misses" on 2 of 12 mutations, both teardown paths. Every missed test passes alone under the mutation, and two identical full-suite runs disagree on *which* tests fail (`PytestUnraisableExceptionWarning`, `ExceptionGroup`): a broken `__exit__`/`aclose` raises at GC time and pytest's unraisable-exception hook attributes it to whichever test is running. That is day-1 finding 2 (second-order fallout from an already-selected failure) made non-deterministic. `bench/mutate.py` now re-runs each miss alone under the same mutation and reports first-order misses (the kill criterion) separately from pollution; `--target path::qualname` reproduces a specific function. The fix for the pollution class remains fork-per-test isolation, not selection.
2. **Per-tree evidence is what makes recording pay.** The first cut unioned the diff over all contributing runs and could only drop a file when *every* run had seen its current content, so the first re-edit of a dirty file escalated to `run_all` through the import-time rule — with recording on, one edit-run-edit cycle collapsed selection until the next full run. Storing the content of dirty `.py` files (content-addressed blobs — only the files being edited) and diffing each test against the tree of the run that last observed it turned that into the 516 → 464 → 1 sequence above.
3. **Test-module changes never run everything.** Appending or deleting a test function used to count a blank line as a module-level change in an import-time file → `run_all`. Blank and comment lines are ignored, a test module's changes select that module, and a module whose known tests can't all be found statically (deleted, renamed; `unittest.TestCase` subclasses are recognised) is run as a whole file — a deleted test never becomes a nonexistent node id handed to pytest.
4. **in == out is two checks, and both were missing.** `run` now reports `unrun_targets` and `complete` — a module skipped at collection or a vanished node id is "incomplete", not "0 failed" — and, with recording on, `record_ok` checks that the journal saw exactly what ran. `mapdb.connect` no longer writes on the read path; before, every `affected` took the write lock.
5. **Scope classification is the retirement safety.** Only a `full` run — no positional or keyword restriction, not xdist, every collected item observed — retires tests missing from the modules it collected; `-x`, `--lf`, node ids, subdirectories, `--collect-only`, an empty session and xdist workers/controller all record as partial. Tests in modules that failed to collect keep their last evidence, which is exactly the evidence that surfaces the breakage.

## Not done

- Evidence resolves per *tree*, not per test, and the collection pseudo-test is a union across runs: an import-time change in any tree still runs all.
- The daemon's warm-up doesn't record; the child does when `run` asks (recording is the default).
- A journal shared across worktrees and agents (the seam for the paid layer in [[market-tia-service]]).
