# ztest — parallel-first pytest-compatible runner

A Linux-first test runner that collects existing pytest tests **once**,
creates warm Python workers from that collected state by forking, schedules
tests dynamically, and returns normal pytest-compatible results.

Real pytest remains the compatibility kernel: configuration, plugin loading,
assertion rewriting, collection, fixtures, parametrization, setup, test
execution, teardown and failure creation all happen inside genuine pytest.
The runner only replaces *process topology and scheduling* — the part
`pytest-xdist` implements by re-collecting the whole suite in every worker.

```
┌─────────────────────────────────────────────────┐
│                  ztest — Zig                    │
│  CLI · scheduler · worker supervisor            │
│  timeout/crash handling · output aggregation    │
└───────────────┬───────────────────────┬─────────┘
                │ control socket        │ events
┌───────────────▼───────────────────────▼─────────┐
│             Python pytest host                  │
│  loads config/plugins/conftest, rewrites        │
│  assertions, collects every test exactly once   │
└───────────────────┬─────────────────────────────┘
                    │ fork after collection
          ┌────────┬┴───────┬────────┐
          │worker 1│worker 2│worker N│   warm pytest, no re-import,
          └────────┴────────┴────────┘   no re-collection
```

## Status (what exists today)

| Milestone | State |
|---|---|
| M0 benchmark harness + correctness oracle | done (`benchmarks/`, `ztest_py/reporting.py`) |
| M1 pure-Python collect-once prefork prototype | done, parity-verified (`ztest_py/prototype.py`) |
| M2 versioned frame protocol (Python + Zig) | done (`ztest_py/protocol.py`, `src/protocol.zig`) |
| M3 Zig controller vertical slice | done (`ztest run -j N -- …`) |
| M4 dynamic queues + `nextitem` lookahead | done (built into M1/M3 from the start) |
| M5 SQLite history / duration scheduling | not started (`src/database.zig` stub) |
| M6 fixture-affinity scheduling | not started — known gap, see benchmarks |
| M7 clean snapshot server | not started (`ztest_py/snapshot.py` stub); workers currently fork from the host |
| M8+ plugin levels, retries, changed-test selection | not started |

See `RESULTS.md` for the decisive-experiment numbers.

## Usage

Pure-Python prototype (no Zig needed):

```bash
export PYTHONPATH=/path/to/ztest/python
python -m ztest_py run -j auto -- tests/ -q
```

Zig controller (pin: Zig 0.16.0; `pip install ziglang==0.16.0` works):

```bash
cd ztest && python -m ziglang build       # or: zig build
export PYTHONPATH=/path/to/ztest/python
./zig-out/bin/ztest run -j auto -- tests/ -q
```

Everything after `--` is passed to pytest unchanged.

## How it works

1. The Zig binary creates a Unix control socket and starts
   `python -m ztest_py host --socket … -- <pytest args>`.
2. The host runs normal pytest startup and collection, then sends a compact
   manifest (integer test ids → node ids) over the socket.
3. The controller tells the host to fork N workers. Each worker inherits the
   fully warmed interpreter — imports, plugins, rewritten-assertion modules,
   collected items — and opens its own connection to the controller.
4. Workers execute assigned test indexes through the real
   `pytest_runtest_protocol` hook and stream serialized setup/call/teardown
   reports back.
5. **`nextitem` lookahead:** a worker never starts a test until its successor
   is queued (or `NO_MORE_TESTS` arrived), so pytest tears down high-scope
   fixtures exactly as it would in a serial run.
6. Crashed workers are detected by connection EOF; their outstanding tests
   are requeued once onto a replacement worker, then reported as crash
   failures if they kill that one too.

In the pure-Python prototype (`ztest_py/prototype.py`) the same worker code
runs against an in-process controller over socketpairs, and reports are
replayed through `pytest_runtest_logstart/logreport/logfinish` in the
parent, so the terminal output, plugins and exit status behave as usual.

## Wire protocol

Little-endian frames: `magic "ZTST" u32 · version u16 · type u16 ·
payload_size u32 · sequence u64 · JSON payload`. Sequence numbers are
per-connection and strictly monotonic; payload size is bounded; unknown
versions and message types are hard errors. See `ztest_py/protocol.py` and
`src/protocol.zig` (kept in lockstep, both unit-tested).

## Testing

```bash
export PYTHONPATH=$PWD/python
python -m pytest tests -q          # protocol, parity, chaos, integration
python -m ziglang build test       # Zig unit tests
```

The parity suite runs the compatibility corpus (classes, parametrization,
fixtures at all scopes, yield finalizers, setup/teardown failures, skip,
xfail/xpass, capsys, monkeypatch, tmp_path, threads, subprocesses, native
imports) under vanilla pytest and under the prefork engine at several worker
counts and batch sizes, and asserts identical collected node IDs, phase
outcomes, failure fingerprints (type + location) and exit codes, with no
missing or duplicated tests.

## Benchmarks

```bash
python benchmarks/generate.py all --out testbeds
python benchmarks/bench.py --suite testbeds/tiny --jobs 4 --repeat 3 --out results/tiny.json
```

## Known limitations (deliberate, per plan)

* Session/module fixtures initialize once **per worker** (same semantics as
  pytest-xdist).
* Fixture-affinity scheduling is not implemented yet; suites dominated by
  expensive module/class fixtures can be slower than `--dist=worksteal`
  until Milestone 6.
* Prefork requires fork-safety: the host refuses to fork when extra threads
  or a running event loop are detected. Spawn-mode fallback is Milestone 7+.
* Linux + standard CPython only.
