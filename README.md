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

prongs requires Python 3.11 or later. From 3.12 it records with `sys.monitoring`; on 3.11 it falls back to a slower profile function.
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
On Python 3.11, which has no `sys.monitoring`, it uses a profile function: the same record, at a higher cost.
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

**Selection.** A changed file is compared unit by unit, not line by line.
The units are each function body and each statement of the module and of its classes, fingerprinted from the AST.
Changed functions map to tests through the inverted map.
Each selected test carries a reason:

| Reason | When |
|---|---|
| `touches changed function` | the test entered a function whose body or signature changed |
| `imports/touches changed file` | the file was added, deleted, changed at module level, or changed mid-run, so it is taken whole |
| `changed function not in the map` | a new function something may already reach (a method, a decorated function) in a known file: every test that touches the file runs |
| `test file changed` / `new test file` | the test module changed beyond the body of a known test |
| `recent status change` | the outcome flipped within the last `history_window` rollups |
| `unmapped test (conservative)` | the map holds no dependency evidence for it |
| `evidence commit ... is not in this repository` | there is nothing to diff its evidence against |

Some changes run everything (`mode: "run_all"`).
These include a changed `conftest.py`, a changed tracked non-Python file no test was seen reading, and module-level or import-time code in a production module.
A new file that did not exist when the map was recorded also runs everything.
Files that cannot affect test outcomes are never a change: `*.md`, `*.rst`, `*.txt`, `*.lock`, `py.typed`, `docs/`, `.github/`, `.gitignore` and `LICENSE`.
Add your own with glob patterns, matched against the path and against the file name:

```toml
[tool.prongs]
inert = ["*.svg", "assets/*"]
```

These patterns never match a `.py` file: a changed module is always analyzed.

As a safety net, a map more than `max_map_age` commits behind HEAD runs everything.

**What counts as a change.** The unit comparison borrows two ideas.
From [Salsa](https://github.com/salsa-rs/salsa): a changed input matters only if the value derived from it changed.
From [pytest-testmon](https://github.com/tarpas/pytest-testmon): the derived value is a checksum of the code's AST, one per block.

- **Formatting is not a change.** Whitespace, comments, wrapped lines, and `import a` to `import a as a` leave every fingerprint equal, so nothing is selected.
- **A function body is its own unit.** Editing a nested function changes neither its parent nor the statement that defines it.
- **A test selects itself.** Editing the body of one known test selects that test, not its module. A changed fixture, helper, decorator or module-level statement still selects the module.
- **A new name nothing observes selects nothing.** A new plain function, a new constant with a plain value, or a new import binds a name the old module never bound and never looked up. The code that uses the new name is a change of its own and is selected as one.
- **A removed import nothing refers to selects nothing.** That holds when the module no longer uses the name, no other file takes it from this module, and the imported module is still imported elsewhere.
- **Reordered imports and functions select nothing.** Reordering any other statement is a module-level change.
- **A changed function no test enters selects nothing**, when the diff is taken from the tree the map was recorded on. From an explicit `--base` the map may be older than the function, so its file is broadened as before.

Whenever one of the "selects nothing" rules for bindings is used, `prongs run` first collects the whole suite without running it.
A new binding still executes at import; if collection fails, everything runs and the errors are reported.

Everything else at module or class level is still a module-level change: a changed assignment, a class statement, a class attribute, a new method, a decorated function, a default or value that calls project code.
The result lists what the analysis decided under `units`: `module_level` (why a file was taken whole), `additive` (bindings judged unobserved) and `unexecuted` (changed functions no test enters).

**Evidence beyond function entry.** A test can depend on the tree without entering a changed function.
The recorder keeps four more kinds of evidence, as dependencies of the test like any other:

| Evidence | Recorded when | Effect on selection |
|---|---|---|
| shared fixtures | a module-, class- or session-scoped fixture is set up | what its setup entered is credited to every test that uses the fixture, not only the first |
| files read | a test opens a tracked non-Python file | a change to that file selects its readers instead of running everything; a file tests read is never inert by default, wherever it lives |
| names listed | a test calls `dir()` or `vars()` on a project module or class (`inspect.getmembers` does) | a name added to or removed from that module or class selects it |
| processes started | a test starts a process | a Python child is recorded and its functions credited to the test; a test whose children could not all be recorded runs on any production change |

Child processes are recorded through `prongs.pth`, which the wheel installs next to the package.
It does nothing unless a recording session set `PRONGS_CHILD`; an editable install does not carry it.
A child that is killed, or is not Python, reports nothing, and its test keeps the marker.
Name programs that run no project Python in `native_commands`: a test that starts only those is selected by a changed non-Python file, not by changed code.

```toml
[tool.prongs]
native_commands = ["git", "node"]
```

A non-Python file no test was ever seen reading still runs everything, unless it is inert.

These rules cannot see a file read by a C library or by a child that is not Python, a module's names taken from `module.__dict__` directly, or a process started while collecting.
Run `prongs audit` on your suite to check them.

**Import-time code.** A changed module-level statement, class attribute, or function that a module body calls runs everything by default: function entry cannot say which tests use the objects that code built.
With `import_state = true`, prongs compares what import *produced* instead.

- A recorded run stores a fingerprint of every project module global and every project class attribute, taken after collection in a fresh process. Functions are fingerprinted without their code, since a changed body is found by the tests that enter it.
- When a change would run everything only for an import-time reason, the suite is collected once (nothing runs) and fingerprinted again.
- Selected are the tests the other rules select, plus those that can reach what changed: tests that entered a function whose source names a changed global, a changed class or one of its subclasses, or anything that holds a changed class or an instance of one; and tests that entered a method of a changed class.
- A changed function body that leaves import's result as it was selects only the tests that enter the function.
- If the tree does not import, everything runs and reports the error.

- If a changed module is in no snapshot to compare with, everything runs and says which.

The result's `import_state` block lists the run-all reasons it overrode and what changed.
A snapshot covers the modules the whole suite imports, whichever tests the run executed, and `prongs ci save` and `ci restore` carry the snapshots with the map and the journals.
What differs between two imports of the same tree (a lock, a timestamp, a cache keyed by a salted hash) is found by taking the stored snapshot twice, and is never compared.
The snapshot processes start from the environment the test session started in, not the one its tests left behind.

```toml
[tool.prongs]
import_state = true      # or "shadow": keep running everything, and report what selecting would skip
```

This rule cannot see state that no project global or class reaches (a registry inside a third-party library, the contents of an `lru_cache`), or a name reached only through a computed string.
It needs the diff to come from the tree the map was recorded on, so it does not apply with `--base`.

A docstring is data a program can read, so a docstring edit is a change by default.
Set `ignore_docstrings = true` to treat docstring edits as formatting.

**Flaky tests.** A test that both passed and failed on one tree within `flaky_window` rollups is flaky.
Flaky tests still run.
A failing flaky test is re-run alone up to `flaky_retries` times.
A pass reports it under `flaky`; failing every time counts it as a failure.

**Order-dependent tests.** A selected run is the suite with tests left out, so a test can meet state that an earlier test left behind and that, in the full suite, a test in between tidies up.
The failures of a selected run are therefore run once more, in a fresh process that holds only them.
What the change broke fails there too, and is a failure.
A test that passes there is reported under `order_dependent` and does not fail the run; the pass is recorded next to the failure, so the test is flaky from then on.
This cannot see a change that makes one test break another through shared state: the full run on the default branch finds that.
Set `isolate_failures = 0` to count every failure as one.

Selected tests run in the order pytest is handed them: whole files first (those in a parent directory before those below it), then single tests.
A parametrized test is handed over as its function, with the known parametrizations that are not selected deselected (`deselect` in the result), never as `name[id]`: an id built from a timestamp or a process id is different in the next run, and pytest refuses a whole run over one id it cannot find.
The `conservation` block counts such an id as `renamed`.

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
| `prongs shadow` | Summarize the shadow audits of runs that executed everything |
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
isolate_failures = 1 # re-run a selected run's failures without the other selected tests (0: off)
ignore_docstrings = false  # true: a docstring edit is not a change
native_commands = []       # programs that run no project Python: "git", "node"
import_state = false       # true: select by what import produces; "shadow": only report
```

| Variable | Meaning |
|---|---|
| `PRONGS_HISTORY_WINDOW`, `PRONGS_MAX_MAP_AGE`, `PRONGS_FLAKY_WINDOW`, `PRONGS_FLAKY_RETRIES`, `PRONGS_ISOLATE_FAILURES` | Override the settings above |
| `PRONGS_IGNORE_DOCSTRINGS=1` | Like `ignore_docstrings = true` |
| `PRONGS_IMPORT_STATE=1` or `=shadow` | Like `import_state = true` or `"shadow"` |
| `PRONGS_COV=1` | Turn the recorder on, like `--prongs-cov` |
| `PRONGS_RECORD_CHILDREN=0` | Do not record inside child processes: every test that starts one keeps its process marker |
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
A pull-request job that checks out the branch head, not the forge's test merge commit, can also restore its own earlier journals: they are this line of history's evidence, so a later push runs only what changed since the last one.
With `import_state` on (set it in `[tool.prongs]`, or as `PRONGS_IMPORT_STATE=1` for every job), the artifacts also carry the import-state snapshots, so a pull request that changes import-time code is selected from, not run whole.
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

## Shadow audits

A run that executes everything is a free audit of the rules that would have skipped tests.
Whenever `prongs run` runs everything because a run-all rule fired, its result carries a `shadow` block:
what the remaining rules would have selected, and every skipped test whose outcome differs from the map's.
Each such run appends one line to `.prongs/shadow.jsonl`, and `prongs shadow` summarizes them by rule and by run-all reason.
A difference is a miss of the rule, a flake, or fallout from a selected failure: read it before trusting or relaxing a rule.

With `import_state = "shadow"`, the report carries a second rule, `import_state`: what selecting by import state (see "Import-time code" above) would have skipped.

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
