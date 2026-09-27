# bolttest

Agent-native test runner for Python. `bolttest` records which source lines each
test executes, then on a change runs only the tests that can be affected,
with a receipt for every test it skipped.

```
pip install bolttest

python -m pytest --bolttest-cov     # one full run with the recorder on
bolttest affected                   # what a change can affect, and why
bolttest run                        # run those tests; exit 1 fail, 2 error, 3 books do not balance
bolttest audit                      # shadow mode: selection checked against a full run
```

See `examples/github-actions.yml` for the CI flow, and `bolttest --help`.
