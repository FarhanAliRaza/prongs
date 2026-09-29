# prongs

prongs is a test runner for Python, built on pytest, for coding agents and developers iterating on large test suites.
It records which project functions each test executes.
After a change, it runs only the tests that change can affect.
Every skipped test comes with a receipt that says why it was safe to skip.

## Why

- **A full suite per edit is too slow.** prongs diffs your change against a recorded map and runs the affected tests only.
- **Agents re-run everything because they cannot tell what is safe to skip.** Every `prongs run` prints one JSON document with a skip receipt: the rule, the evidence, and how old it is.
- **Selection tools are hard to trust.** Every run checks that collected tests equal selected plus run-all plus skipped. If the books do not balance, the run fails with exit code 3.
- **Trust needs measuring.** `prongs audit` runs the full suite in shadow mode and reports every status change that selection would have skipped.

## Installation

```console
pip install prongs
```

prongs requires Python 3.12 or later, since it records with `sys.monitoring`.
It has no dependencies beyond pytest in your environment.
It registers itself as a pytest plugin through the `pytest11` entry point.
The recorder stays off unless you pass `--prongs-cov`, so plain pytest runs are unaffected.

## Quick start

Run from the repository root, the directory you run pytest from.

```console
python -m pytest --prongs-cov   # one full run with the recorder on
prongs affected                 # what a change can affect, and why
prongs run                      # run those tests and report as JSON
```

The first command writes a journal file under `.prongs/`.
`affected` and `run` fold it into the map before they select.
Add `.prongs/` to your `.gitignore`.

After editing one function, `prongs affected` prints (trimmed):

```json
{
  "mode": "select",
  "n_total": 98,
  "n_selected": 3,
  "n_skipped": 95,
  "changed_functions": [
    "src/prongs/__main__.py::exception_line",
    "src/prongs/config.py::settings"
  ],
  "run_all_reasons": [],
  "selected_by_reason": {
    "touches changed function src/prongs/config.py": 1,
    "touches changed function src/prongs/__main__.py": 2
  },
  "tests": [
    "tests/test_audit.py::test_a_control_run_separates_environment_drift_from_misses",
    "tests/test_run.py::test_exception_line_skips_pytest_hints",
    "tests/test_run.py::test_group_failures_keeps_one_traceback_per_root_cause"
  ],
  "history": {"window": 3, "recent_changes": 0, "flaky": 0, "flaky_window": 50},
  "evidence": {"runs": 2, "last_full_run": {"id": 1, "scope": "full", "commit": "41e7c5c8..."}, "...": "..."},
  "skip_receipt": {
    "skipped": 95,
    "rule": "a test is skipped only when, relative to the tree of the run that last observed it, no changed function or file is in its recorded dependency set; ...",
    "map_age": {"commit": "41e7c5c8", "commits_behind_head": 1, "max_map_age": 50},
    "unmapped_tests": 0,
    "warnings": []
  }
}
```

With no map, `prongs run` runs the whole suite and records it, so it builds the map it lacked.

## How it works

**Map.** The pytest plugin uses `sys.monitoring` to record the project functions each test enters.
Code that runs at import or collection time is recorded as its own set.
Stdlib and site-packages code is disabled on first sight and costs nothing after that.

**Journal and roll-up.** Every recorded session appends one journal file and never touches the map.
`prongs rollup` folds pending journal files into `.prongs/map.sqlite`.
`affected`, `run` and `audit` roll up first, unless you pass `--no-rollup`.
Partial runs (node ids, `-k`, `-m`, xdist workers) refresh exactly the tests they observed.
Concurrent sessions each write their own file.

**Provenance.** Each run stores HEAD and a content hash of every file that differed from HEAD, at start and finish.
The selector diffs against the tree the evidence actually saw, not the tree you assume.
A dirty file that still matches what the run saw is not a change.

**Selection.** Changed lines map to functions via AST spans, then to tests through the inverted map.
Each selected test carries a reason:

| Reason | When |
|---|---|
| `touches changed function` | the test entered a function whose lines changed |
| `imports/touches changed file` | the file was added, deleted, changed at module level, or changed mid-run, so it is taken whole |
| `changed function not in the map` | a new function in a known file: every test that touches the file runs |
| `test file changed` / `new test file` | the test module itself changed |
| `recent status change` | the outcome flipped within the last `history_window` rollups |
| `unmapped test (conservative)` | the map holds no dependency evidence for it |
| `evidence commit ... is not in this repository` | there is nothing to diff its evidence against |

Some changes run everything (`mode: "run_all"`).
These include a changed `conftest.py`, a changed tracked non-Python file, and module-level or import-time code in a production module.
A new file that did not exist when the map was recorded also runs everything.
Files that cannot affect test outcomes are never a change: `*.md`, `*.rst`, `*.txt`, `*.lock`, `docs/`, `.github/`, `.gitignore` and `LICENSE`.
Add your own with glob patterns, matched against the path and against the file name:

```toml
[tool.prongs]
inert = ["*.svg", "assets/*"]
```

These patterns never match a `.py` file: a changed module is always analyzed.

As a safety net, a map more than `max_map_age` commits behind HEAD runs everything.

**Flaky tests.** A test that both passed and failed on one tree within `flaky_window` rollups is flaky.
Flaky tests still run.
A failing flaky test is re-run alone up to `flaky_retries` times.
A pass reports it under `flaky`; failing every time counts it as a failure.

**Books that balance.** Every `run` result carries a `conservation` block.
It checks `collected == selected + run_all + skipped`, and that every planned test produced a result.
Any mismatch makes the status `inconsistent` and the exit code 3.
A run that silently did less than asked is never reported as `passed`.

## Commands

All commands print one JSON document on stdout, except `daemon`.

| Command | Purpose |
|---|---|
| `prongs affected` | Show what would run, and why |
| `prongs run` | Select, execute, and report results with receipts |
| `prongs audit` | Select, then run the full suite and report every skipped status change |
| `prongs rollup` | Fold pending journal files into the map |
| `prongs ci save DIR` | Write the map and this job's journal files as a CI artifact |
| `prongs ci restore PATH...` | Install CI artifacts into a fresh clone |
| `prongs daemon` | Start the warm daemon in the foreground |
| `prongs stop` | Stop the daemon |

`affected`, `run` and `audit` share these flags:

| Flag | Default | Meaning |
|---|---|---|
| `--base REV` | evidence commits | Diff against `REV` instead of the trees the evidence was observed on |
| `--rollup` / `--no-rollup` | on | Fold pending journal files into the map first |
| `--history-window N` | 3 | Select tests whose outcome flipped in the last N rollups |
| `--max-map-age N` | 50 | Run everything when the map is more than N commits behind HEAD |
| `--flaky-window N` | 50 | Flaky evidence older than N rollups expires |

`run` adds:

| Flag | Default | Meaning |
|---|---|---|
| `--record` / `--no-record` | on | Append this run to the journal |
| `--cov` / `--no-cov` | on | Record per-test coverage; `--no-cov` still appends outcomes and durations |
| `--flaky-retries N` | 2 | Re-run a failing flaky test alone up to N times; 0 makes a flaky failure a failure |

`audit` adds:

| Flag | Default | Meaning |
|---|---|---|
| `--pytest-args ARGS` | `$PRONGS_RUN_ARGS` | Extra pytest arguments for the full run |
| `--isolate` / `--no-isolate` | on | Re-run each miss alone to separate first-order misses from pollution |
| `--record` / `--no-record` | off | Append the full run to the real journal |
| `--control` / `--no-control` | off | Re-run each first-order miss on its baseline commit, checked out in place (clean tree only) |

`ci save` takes `--map` / `--no-map` and `--journals` / `--no-journals`, both on by default.
`ci restore` takes `--fetch` / `--no-fetch` (on) to deepen a shallow clone, and `--remote NAME` (default `origin`).

The pytest plugin adds:

| Option | Meaning |
|---|---|
| `--prongs-cov` | Record per-test coverage and outcomes as a journal file |
| `--prongs-journal DIR` | Journal directory for `--prongs-cov` |
| `--prongs-journal-key KEY` | Name of this run's journal file |

`run` uses the daemon when its socket exists, and otherwise runs pytest in-process.
If the warm image is stale, it falls back to a cold run and says so under `executor`.

### Exit codes

| Code | `run` | `audit` | Other commands |
|---|---|---|---|
| 0 | passed, or nothing to run | no miss | done |
| 1 | tests failed | a first-order miss | |
| 2 | error: pytest exit 2-5, a collection error, a crash, an unknown revision | error, or no map to audit | error |
| 3 | inconsistent: the books do not balance | | |

## Configuration

Settings are read from `[tool.prongs]` in `pyproject.toml`, then `PRONGS_<NAME>` environment variables, then command-line flags.

```toml
[tool.prongs]
history_window = 3   # a test whose outcome flipped in the last N rollups is selected
max_map_age = 50     # a map more than N commits behind HEAD runs everything
flaky_window = 50    # flaky evidence older than N rollups expires (0: nothing is flaky)
flaky_retries = 2    # re-run a failing flaky test alone up to N times
```

| Variable | Meaning |
|---|---|
| `PRONGS_HISTORY_WINDOW`, `PRONGS_MAX_MAP_AGE`, `PRONGS_FLAKY_WINDOW`, `PRONGS_FLAKY_RETRIES` | Override the settings above |
| `PRONGS_COV=1` | Turn the recorder on, like `--prongs-cov` |
| `PRONGS_DIR` | State directory, relative to the repository root (default `.prongs`); point worktrees at one directory to pool runs |
| `PRONGS_JOURNAL` | Journal directory only (default `<state dir>/journal`) |
| `PRONGS_JOURNAL_KEY` | Name of the recorded run's journal file |
| `PRONGS_RUN_ARGS` | Extra pytest arguments for every pytest run prongs starts: `run`, `audit`, flaky retries, and daemon children |
| `PRONGS_WARM_ARGS` | Extra pytest arguments for the daemon's warm-up collection |
| `PRONGS_WARM_DB_TEST` | A test node id the daemon runs once at warm-up, so forked children inherit the test database |

## CI

A CI job starts from a fresh clone, so the map travels as an artifact.
Pushes to the default branch run the full suite with the recorder and save the map to a cache.
Pull requests restore that map, run `prongs run` as the gate, and upload their journal files.
The next push job folds those journals in as history, which feeds flaky detection.
`ci restore` deepens a shallow clone until the map's evidence commits are present.
With no map, `prongs run` runs everything and records it, so the gate never depends on the cache.
See [`examples/github-actions.yml`](examples/github-actions.yml) for the full, commented workflow.

```yaml
- name: Restore
  run: python -m prongs ci restore "$RUNNER_TEMP/prongs-ci"

- name: Tests (the affected ones)
  if: github.event_name == 'pull_request'
  shell: bash
  run: python -m prongs run | tee "$RUNNER_TEMP/prongs-result.json"

- name: Tests (all of them, recorded)
  if: github.event_name != 'pull_request'
  run: python -m pytest --prongs-cov

- name: Save this job's journals
  if: always() && github.event_name == 'pull_request'
  run: python -m prongs ci save --no-map "$RUNNER_TEMP/prongs-out"

- name: Save the map
  if: always() && github.event_name != 'pull_request'
  run: python -m prongs ci save --no-journals "$RUNNER_TEMP/prongs-ci/map"
```

## Audit mode

`prongs audit` selects exactly as `prongs run` would, then runs the full suite with the recorder on.
The full run goes to a temporary journal, so an audit never feeds the map it audits.
It compares every test's status with the map's baseline.
A test whose status changed and was not selected is a candidate miss, reported with the receipt that skipped it.

Each candidate is re-run alone on the same tree:

- **First-order miss:** it still differs from its baseline alone. This is the selector's fault.
- **Second-order pollution:** it returns to its baseline alone. An already-selected failure caused it, not selection.

A known-flaky test that flips is reported under `flaky`, not as a miss.
With `--control`, a miss that also differs on its baseline commit is reported as `drift`.

```console
prongs audit --base HEAD~1
```

The audit exits 1 on any first-order miss.
Run it in CI alongside selection, and treat a first-order miss as the kill criterion for trusting selection on your suite.

## Using it from an agent

`prongs run` is designed as the single command an agent calls after an edit.
It rolls up, selects, executes, and reports in one JSON document.
The output is built for a token budget: failures are grouped by exception line, tracebacks are truncated, passes are counted, not listed.
The skip receipt and `evidence` let the agent decide whether to trust the skip.
The exit code carries the verdict, so no output parsing is needed to gate on it.

## Status

prongs is early (0.0.x).
The command-line interface, JSON output shape and map schema may change between releases.
The `bench/` directory holds the harnesses used to validate selection: a baseline timer, mutation testing and history replay around `audit`, and a local replay of the CI artifact flow.

## License

MIT. See [LICENSE](LICENSE).
