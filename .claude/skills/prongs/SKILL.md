---
name: prongs
description: Run only the tests a code change can affect, using prongs (a pytest-based selective test runner with skip receipts). Use this whenever you edit Python in a repository that has prongs installed or a `.prongs/` directory, instead of running the full pytest suite. Also use it when asked to "run the affected tests", "check what my change breaks", "which tests should run", to audit or trust test selection, to set up prongs in CI, or to build the dependency map for the first time.
---

# prongs

prongs records which project functions each test executes, then after a change runs only the tests that change can affect.
Every run prints one JSON document with a skip receipt: the rule, the evidence, and how old it is.
The exit code carries the verdict, so gate on it rather than parsing output.

Run every command from the repository root, the directory pytest is normally run from.
Prefer `python -m prongs ...` over `prongs ...` when unsure the console script is on PATH.

## The loop after an edit

1. Edit the code.
2. Run `python -m prongs run`.
3. Read the exit code and the `status` field. Fix failures, then run again.

```console
python -m prongs run
```

This rolls up pending journal files, selects the affected tests, runs them, records the run, and reports.
That is the single command to call after each edit. Do not fall back to a full `pytest` run unless prongs tells you to (see below).

To see what would run without running it:

```console
python -m prongs affected
```

## Exit codes

| Code | `run` | `audit` | Other commands |
|---|---|---|---|
| 0 | passed, or nothing to run | no miss | done |
| 1 | tests failed | a first-order miss | |
| 2 | error: pytest exit 2-5, a collection error, a crash, an unknown revision | error, or no map to audit | error |
| 3 | inconsistent: collected != selected + run_all + skipped, or a planned test produced no result | | |

Exit 3 means prongs refused to call a run `passed` because the books did not balance. Report it, do not retry blindly.

## Reading the output

The `run` document contains:

- `status`: `passed`, `failed`, `nothing_to_run`, `error`, or `inconsistent`.
- `mode`: `select` (only affected tests ran) or `run_all` (everything ran). `run_all_reasons` says why.
- `n_total`, `n_selected`, `n_skipped`, `changed_functions`, `selected_by_reason`.
- `failures`: grouped by exception line with truncated tracebacks. Passes are counted, not listed.
- `flaky`: known-flaky tests that failed and were retried alone. `counted_as` is `flaky` (a retry passed) or `failure`.
- `skip_receipt`: the rule used, `map_age` (commits behind HEAD), `unmapped_tests`, `warnings`.
- `evidence`: the runs the map is built from, including `last_full_run`.
- `conservation`: the balance check behind exit code 3.
- `executor`: `daemon`, `subprocess`, or a note that the warm image is stale.

Trust a skip when `skip_receipt.warnings` is empty, `unmapped_tests` is 0, and `map_age.commits_behind_head` is small.
If the receipt looks weak, run `python -m prongs audit` (below) rather than running everything by hand.

Selection reasons you will see under `selected_by_reason`:

| Reason | When |
|---|---|
| `touches changed function` | the test entered a function whose lines changed |
| `imports/touches changed file` | the file was added, deleted, changed at module level, or changed mid-run |
| `changed function not in the map` | a new function in a known file: every test touching that file runs |
| `test file changed` / `new test file` | the test module itself changed |
| `recent status change` | the outcome flipped within the last `history_window` rollups |
| `unmapped test (conservative)` | the map holds no dependency evidence for it |

`mode: "run_all"` happens for a changed `conftest.py`, a changed tracked non-Python file, module-level or import-time code in a production module, a file that did not exist when the map was recorded, or a map more than `max_map_age` commits behind HEAD.
Files that cannot affect tests are never a change: `*.md`, `*.rst`, `*.txt`, `*.lock`, `docs/`, `.github/`, `.gitignore`, `LICENSE`.

## Building the map for the first time

With no map, `python -m prongs run` runs the whole suite with the recorder and builds the map it lacked.
Equivalently, run pytest with the recorder on:

```console
python -m pytest --prongs-cov
```

Either writes a journal file under `.prongs/`. `affected`, `run` and `audit` fold pending journals into `.prongs/map.sqlite` before selecting.
Make sure `.prongs/` is in `.gitignore`.

prongs needs Python 3.12 or later. The recorder is off for plain pytest runs unless `--prongs-cov` or `PRONGS_COV=1` is set.

## Diffing against a specific revision

By default prongs diffs against the trees its evidence was recorded on. To compare against a branch point instead:

```console
python -m prongs affected --base main
python -m prongs run --base HEAD~1
```

## Auditing selection

`audit` selects as `run` would, then runs the full suite and reports every status change that selection would have skipped.
It never feeds the map it audits.

```console
python -m prongs audit --base HEAD~1
```

- A **first-order miss** still differs from its baseline when re-run alone. The selector was wrong. Exit 1.
- **Second-order pollution** returns to baseline alone. A selected failure caused it, not selection.
- `--control` re-runs each miss on its baseline commit to separate environment drift (clean tree only).

Use this when asked whether selection can be trusted on a suite, or when a skip receipt looks weak.

## Commands

| Command | Purpose |
|---|---|
| `prongs affected` | Show what would run, and why |
| `prongs run` | Select, execute, record, and report |
| `prongs audit` | Select, then run the full suite and report skipped status changes |
| `prongs rollup` | Fold pending journal files into the map |
| `prongs ci save DIR` | Write the map and this job's journals as a CI artifact |
| `prongs ci restore PATH...` | Install CI artifacts into a fresh clone |
| `prongs daemon` | Start the warm daemon in the foreground |
| `prongs stop` | Stop the daemon |

Shared flags for `affected`, `run`, `audit`: `--base REV`, `--no-rollup`, `--history-window N` (3), `--max-map-age N` (50), `--flaky-window N` (50).
`run` adds `--no-record`, `--no-cov` (outcomes only, no coverage), `--flaky-retries N` (2).
`audit` adds `--pytest-args ARGS`, `--no-isolate`, `--record`, `--control`.

Extra pytest arguments for every run prongs starts go in `PRONGS_RUN_ARGS`, for example `PRONGS_RUN_ARGS="-p no:cacheprovider"`.

## Configuration

Settings come from `[tool.prongs]` in `pyproject.toml`, then `PRONGS_<NAME>` environment variables, then flags.

```toml
[tool.prongs]
history_window = 3
max_map_age = 50
flaky_window = 50
flaky_retries = 2
inert = ["*.svg", "assets/*"]   # extra files that can never affect tests; never matches .py
```

Useful environment variables: `PRONGS_DIR` (state directory, default `.prongs`; share one across worktrees to pool runs), `PRONGS_RUN_ARGS`, `PRONGS_WARM_ARGS`, `PRONGS_WARM_DB_TEST` (a test the daemon runs at warm-up so forked children inherit the test database).

## CI

A CI job starts from a fresh clone, so the map travels as an artifact.
Pushes to the default branch run the full suite with the recorder and save the map.
Pull requests restore the map, run `prongs run` as the gate, and upload their journals so the next push folds them in as history.
With no map, the gate runs everything, so it never depends on the cache.

```yaml
- run: python -m prongs ci restore "$RUNNER_TEMP/prongs-ci"
- if: github.event_name == 'pull_request'
  run: python -m prongs run | tee "$RUNNER_TEMP/prongs-result.json"
- if: github.event_name != 'pull_request'
  run: python -m pytest --prongs-cov
- if: always() && github.event_name == 'pull_request'
  run: python -m prongs ci save --no-map "$RUNNER_TEMP/prongs-out"
- if: always() && github.event_name != 'pull_request'
  run: python -m prongs ci save --no-journals "$RUNNER_TEMP/prongs-ci/map"
```

The full commented workflow is in `examples/github-actions.yml` of the prongs repository.

## When things go wrong

- **Every run is `run_all`.** Check `run_all_reasons`. A changed `conftest.py`, a new file, or a stale map (`map_age`) are the usual causes. Rebuild the map with a full recorded run on the current tree.
- **`unmapped_tests` is large.** The map predates these tests. A full recorded run fixes it.
- **`executor` says the warm image is stale.** Run `python -m prongs stop` and start `python -m prongs daemon` again, or ignore it: the run already fell back to a cold subprocess.
- **A test is reported under `flaky`.** It still runs every time. Fix the flake or raise `--flaky-retries`; set it to 0 to treat flaky failures as failures.
- **Exit 2 with an unknown revision.** The evidence commit is not in this clone. Deepen the clone (`prongs ci restore --fetch` does this in CI) or pass `--base`.
