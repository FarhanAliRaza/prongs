# External review of [[hypothesis]] (July 2026)

> Verbatim verdict from an outside review of the hypothesis + architecture analysis.
> Corrections from this document have been folded into [[hypothesis]] and [[poc-plan]].

## My verdict

This is now pointed at a **real, technically substantial open-source project**. I agree with your instinct: build the hard infrastructure first, make it excellent for developers, and let agents become a high-frequency client of it.

But the final analysis is about **70% right and 30% dangerously overconfident**.

The right architectural boundary is:

> **Keep pytest as the semantic engine. Build an incremental execution runtime around it.**

The wrong claim is:

> “Because real pytest executes the tests, compatibility is free and there is zero compatibility risk.”

Delegating execution to pytest eliminates the need to reimplement assertions, fixture resolution and most runtime hooks. That is an enormous advantage. But collection, process lifetime, scheduling, fixture reuse and worker isolation are all observable parts of pytest semantics. Once you alter those, compatibility risk returns.

I would define the project this way:

> **An incremental runtime for pytest that learns code–test dependencies, maintains disposable warm execution environments, selects tests conservatively, and explains every decision.**

Not an AI watcher. Not a pytest clone. Not merely a Rust CLI.

---

# What the analysis gets right

## 1. Real pytest should remain the semantic authority

This is the central correct decision.

Rust should not resolve fixtures, reproduce hook ordering, rewrite assertions or emulate arbitrary plugins. Python plus real pytest should do those things.

Rust can own:

* process lifecycle;
* file watching and content hashing;
* the dependency database;
* invalidation;
* test planning;
* scheduling;
* IPC and result aggregation;
* persistent local state.

Python should own:

* canonical pytest collection;
* fixture and plugin execution;
* pytest lifecycle hooks;
* runtime dependency instrumentation;
* exact test outcome reporting.

That boundary removes a huge amount of implementation risk.

## 2. Test selection is more important than making individual tests faster

Correct.

For a large suite, eliminating 95% of unnecessary executions is more valuable than shaving 20% from fixture resolution. The open problem is not merely constructing a test map. It is constructing one that users continue to trust after source, configuration, environment and external-resource changes.

That is why “receipts” matter:

* Why was this test selected?
* Why was another skipped?
* How fresh is the evidence?
* Which dependencies were observed dynamically?
* Which dependencies were inferred statically?
* What unknowns caused the run to broaden?

That is technically meaningful infrastructure, not an AI wrapper.

## 3. Agents make latency and machine-readable planning more important

Also correct, with one qualification: agents should be a first-class client, not necessarily the project’s only user.

Charlie Marsh has publicly described running several coding-agent sessions in parallel and losing some confidence in agent-produced changes, while emphasizing the need for tooling that establishes confidence faster. That supports the direction, although it does not prove specific figures such as “10–20 agents per developer” or “80 test runs per task.” Those numbers should remain hypotheses until measured. ([PyDevTools][1])

The same OSS tool should improve:

```bash
fastpytest tests/test_checkout.py::test_retry
```

and expose:

```bash
fastpytest plan --diff HEAD~1 --format json
```

A human gets a much faster feedback loop. An agent gets a reliable machine interface. The underlying runtime is the same.

## 4. Caching belongs near the end

Correct again.

Result caching requires a sufficiently complete model of:

* source dependencies;
* data-file dependencies;
* environment values;
* generated inputs;
* fixture state;
* subprocess behavior;
* external services;
* time, randomness and network access.

Without that, content-addressed caching is just optimistic test skipping. Build the observation and invalidation system first.

---

# Where the analysis is technically wrong

## 1. Static collection is not “outside” pytest compatibility

This sentence should be removed:

> “You can find tests with static parsing instead, no compat needed.”

Collection is part of pytest’s semantics.

Pytest plugins can:

* implement custom collectors;
* generate tests dynamically;
* modify, remove and reorder collected items;
* alter markers;
* create parametrized cases;
* inspect configuration and environment during collection;
* collect non-Python files;
* behave differently according to the `conftest.py` hierarchy.

The official plugin system applies hooks throughout configuration, collection, execution and reporting. In particular, `pytest_collection_modifyitems` runs after collection and can modify the final item list. ([Pytest Documentation][2])

The existing `rtest` project demonstrates the exact boundary. Static AST collection produces impressive collection benchmarks, but the project documents remaining edge cases around dynamic parametrization, generated IDs, plugins, metaclasses and complex codebases. It also admits that some repositories fail under its collector despite working with pytest. ([GitHub][3])

### The safe design

Static collection should be a **speculative index**, not the source of truth.

Use it for:

* instant test search;
* finding likely test modules;
* mapping changed lines to nearby tests;
* identifying candidate node IDs;
* deciding which modules pytest should canonically collect;
* detecting whether a file appears statically simple.

Use real pytest collection for:

* canonical node IDs;
* dynamic parametrization;
* custom collectors;
* hook-modified item lists;
* markers and runtime collection conditions;
* any file whose static and canonical results have previously differed.

A good implementation can classify modules:

```text
STATIC_SAFE
CANONICAL_REQUIRED
UNKNOWN
```

After every canonical collection, compare the static and pytest results. If they differ, permanently classify that module or configuration as dynamic until its relevant inputs change.

So the rule should be:

> **Static analysis proposes. Pytest disposes.**

---

## 2. “Real pytest inside the worker” does not produce zero compatibility risk

This is the most important correction.

`rpytest` already implements almost exactly the proposed architecture:

1. a Python daemon collects the suite;
2. a Rust CLI filters tests;
3. warm Python workers execute them.

Its published benchmark for 480 tests is 2.91 seconds for pytest versus 1.55 seconds for rpytest, approximately 1.9×. ([GitHub][4])

Its README describes full compatibility, but its own compatibility documentation marks CLI support and hooks as partial, excludes or redirects flags such as `--cov`, and documents plugin and collection-order differences. It also notes that fixture timing can differ when fixtures are reused across invocations. ([Neullabs Docs][5])

That is not a criticism of rpytest. It proves that this is a genuinely difficult systems problem.

Even when pytest executes the test function, your orchestration affects:

* how and when plugins initialize;
* which CLI hooks see which arguments;
* test collection order;
* session and module fixture lifetime;
* whether session fixtures exist once per suite or once per worker;
* `pytest-randomly` and order-sensitive suites;
* coverage instrumentation;
* output-capture plugins;
* timeout and process-management plugins;
* plugins that expect to own xdist;
* globals and imported module state;
* teardown behavior;
* signals and subprocesses.

The accurate statement is:

> Delegating canonical collection and execution to pytest greatly reduces the compatibility surface, but any change to process, scheduling or state lifetime remains observable. Uncertain configurations must fall back to a fresh pytest process.

That fallback is not a failure. It is what makes the fast path trustworthy.

---

## 3. A warm daemon needs generations, not one immortal process

Suppose the daemon imports:

```python
app.payments.retry
```

Then the developer edits `app/payments/retry.py`.

The imported module in the daemon has not magically changed. You have three choices:

1. attempt module reloading, which is not generally safe;
2. continue using stale code, which is incorrect;
3. destroy that warm process and create a new generation.

The third is the serious design.

A warm execution generation should be identified by a fingerprint resembling:

```text
Python executable and version
installed distributions and versions
pytest version
loaded plugin set
rootdir and import mode
pytest configuration
relevant environment policy
conftest.py hierarchy
bootstrap modules
imported application-code hashes
worker-mode configuration
```

A source or configuration change invalidates some or all of that generation. When the system cannot confidently determine the affected region, it destroys the generation.

The warm state is therefore:

> **A disposable cache, never authoritative state.**

You could offer several execution levels:

```text
fresh       real pytest in a new process; semantic oracle
warm        cached process generation; no cross-run fixture reuse
optimized   selection, scheduling and explicitly safe reuse
```

This gives you a compatibility ladder instead of one brittle “drop-in” promise.

---

## 4. Forking does not clone a normal Postgres database

This claim is simply incorrect:

> “Every agent gets a pristine copy-on-write DB state for free.”

Copy-on-write applies to memory pages in the forked Python process. A normal Postgres server is another process, usually with its own shared buffers, filesystem and transaction state.

Forking a Python worker may duplicate the client-side file descriptor and connection object. That is generally something to avoid, not a database snapshot.

For external resources you need explicit adapters:

| Resource      | Actual isolation mechanism                                                           |
| ------------- | ------------------------------------------------------------------------------------ |
| Postgres      | database/template clone, per-worker schema, transaction strategy, or database branch |
| Redis         | per-run namespace or dedicated instance                                              |
| HTTP server   | allocated port and process namespace                                                 |
| Filesystem    | per-run temporary root or overlay                                                    |
| Message queue | per-run queue/topic namespace                                                        |
| Object store  | unique bucket prefix or emulator instance                                            |

Forking can isolate:

* ordinary Python objects;
* in-memory caches;
* some in-process fixture state;
* potentially an in-memory SQLite database, subject to connection and threading constraints.

It does not snapshot arbitrary infrastructure.

There is also a broader fork-safety problem. Python 3.14 changed the default POSIX multiprocessing start method from `fork` to `forkserver`, specifically to avoid common incompatibilities in multithreaded processes. ([Python documentation][6])

A warmed Django, asyncio, gRPC, CUDA or native-library process may already contain:

* threads;
* locked mutexes;
* event loops;
* open sockets;
* connection pools;
* background telemetry;
* native runtime state.

Therefore fork-after-import should be:

* Linux-first;
* optional;
* guarded by compatibility detection;
* disabled when unsafe state is observed;
* paired with explicit child reinitialization hooks.

It is a useful optimization, not the universal foundation.

---

## 5. The TDAD result is relevant, but the analysis overstates what it proves

The TDAD paper is promising evidence that supplying test-impact context can improve agent behavior. But it does **not** establish that a warm Rust pytest runtime will cut regressions by 70%.

In its 100-instance phase:

* test-level regression rate fell from 6.08% to 1.82%;
* resolution fell from 31% to 29%;
* patch generation fell from 86% to 74%;
* instance-level regression rate was 30.2% for baseline and 33.3% for GraphRAG.

The improvement was principally in the **severity and number of broken tests**, not in the fraction of generated patches that regressed. ([arXiv][7])

In its separate 25-instance phase, resolution increased from 24% to 32%, but both the baseline and TDAD conditions had a 0% regression rate. ([arXiv][7])

The authors also explicitly identify limitations:

* only 100 and 25 instances;
* no formal significance tests;
* smaller local models;
* Python-only evaluation;
* static analysis unable to capture dynamic dispatch, monkeypatching and runtime-generated code. ([arXiv][7])

Most importantly, the runtime integration was deliberately simple: a static test-map file, a short agent instruction and ordinary `grep` plus pytest. No fast daemon, graph service or custom execution engine was required. ([arXiv][7])

So the defensible conclusion is:

> **TDAD supports exposing concise code–test context to agents. It does not validate the daemon, fork, scheduler or caching architecture.**

That architecture must earn its own evidence through benchmarks and compatibility testing.

---

## 6. `sys.monitoring` is useful, but per-test dependency tracking is not free

PEP 669 and `sys.monitoring` are excellent foundations for a low-overhead recorder. But the coverage.py implementation currently does not support dynamic contexts with its `sysmon` core, and also has limitations involving plugins and some concurrency systems. Dynamic contexts are precisely the usual way to distinguish coverage by individual test. ([Coverage.py][8])

That means the project likely needs its own narrower recorder.

A practical design would:

1. assign an integer ID to every canonical pytest item;
2. set the active test ID at setup/call/teardown boundaries;
3. use `sys.monitoring` function-entry events rather than line events by default;
4. record code-object IDs against the current test;
5. propagate instrumentation into subprocesses;
6. record unattributed activity from background threads;
7. mark a test’s dependency observation incomplete when events cannot be attributed safely.

The resource graph should extend beyond Python execution:

```text
test -> Python function/code object
test -> imported module
test -> opened data file
test -> subprocess executable
test -> selected environment input
test -> fixture
test -> external-resource class
```

Not all of those need to ship in the first release. But this is where the genuinely difficult and differentiating engineering lives.

---

## 7. Work stealing is no longer novel by itself

The claim that xdist is only using naive round-robin scheduling is out of date. Current xdist includes a `worksteal` distribution mode and exposes a hook for custom scheduling implementations. ([pytest-xdist][9])

You can still improve on it, but the scheduler needs a stronger thesis:

* historical-duration awareness;
* fixture-affinity grouping;
* resource constraints;
* isolation requirements;
* failure-first prioritization;
* critical-path optimization;
* agent-provided time budgets;
* topology-aware distribution across machines.

For example, arbitrarily splitting tests that share an expensive module fixture may cost more than it saves. A useful scheduler understands both duration and setup topology.

---

## 8. Two factual corrections belong in the notes

`pytest-testmon` is currently MIT-licensed, not AGPL. Its own repository explicitly offers a no-deselection adoption mode so projects can first evaluate reliability and savings. ([GitHub][10])

Also, as of July 12, 2026, OpenAI’s official announcement still says it **plans to acquire Astral**, with closing subject to conditions; it does not state on that page that the transaction had already closed. The direction toward Codex verification tooling is real, but the “12–24 month window before a free pytest runner” is speculation, not evidence. ([OpenAI][11])

Neither correction invalidates the opportunity. They make the hypothesis more credible.

---

# What I would actually build

## The open-source project

Call the category:

> **Incremental pytest runtime**

The architecture:

```text
                  Rust CLI
                     |
              Rust daemon/service
        +------------+-------------+
        |            |             |
  session manager  planner      scheduler
        |            |             |
        +------ dependency DB ------+
                     |
               worker protocol
                     |
           Python worker generation
                     |
          real pytest + bridge plugin
          /          |             \
 canonical      dependency       structured
 collection      recorder          events
```

### Rust owns

* daemon and CLI;
* session fingerprints;
* file hashing and watching;
* dependency graph storage;
* reverse indices;
* test-impact planning;
* worker lifecycle;
* scheduling;
* event aggregation;
* JSON/JSONL protocol.

### Python owns

* real pytest startup;
* canonical item collection;
* plugin loading;
* fixture execution;
* test setup/call/teardown;
* monitoring callbacks;
* exact reports and exceptions.

### Static AST analysis owns

* fast approximate indexing;
* changed-range-to-function mapping;
* import graph supplementation;
* candidate-test discovery;
* collection invalidation hints.

It does **not** own canonical test identity.

---

# The dependency database is the technical core

I would not start by building a generic coverage product. I would build the recorder as part of one complete vertical slice:

```bash
fastpytest record
fastpytest plan --diff HEAD~1
fastpytest run --diff HEAD~1
fastpytest explain tests/payments/test_retry.py::test_retry
```

A minimal data model could be:

```text
sessions
tests
code_units
resources
test_dependencies
runs
test_results
collection_snapshots
generation_fingerprints
```

For fast selection, assign integer IDs and maintain reverse bitmaps:

```text
code_unit_id -> bitmap(test_ids)
resource_id  -> bitmap(test_ids)
```

Planning a diff then becomes approximately:

```text
changed resources
    -> reverse dependency lookups
    -> bitmap unions
    -> policy broadening
    -> ordered execution plan
```

SQLite in WAL mode is sufficient initially. A graph database would add operational complexity before it adds value.

---

# What “receipts” should mean

Do not claim that you can mathematically prove every skipped test is unaffected in dynamic Python.

A receipt should instead expose the auditable basis of the decision:

```json
{
  "graph_generation": "7f93ab2",
  "selected": [
    {
      "nodeid": "tests/payments/test_retry.py::test_idempotent_retry",
      "reasons": [
        {
          "kind": "dynamic_code_dependency",
          "resource": "app/payments/retry.py:RetryPolicy.execute",
          "last_observed_run": 4812
        }
      ]
    }
  ],
  "broadened": [
    {
      "scope": "tests/payments/",
      "reason": "tests/payments/conftest.py changed"
    }
  ],
  "forced": [
    {
      "scope": "unmapped_tests",
      "reason": "dependency history incomplete"
    }
  ],
  "confidence": "conservative"
}
```

The conservative rules should include:

* changed `conftest.py` → run its entire collection scope;
* changed pytest configuration → recollect and broaden;
* changed dependency lockfile or plugin version → create a fresh generation;
* new test → always run;
* unmapped test → always run;
* changed production file with no reverse edges → package or repository fallback;
* incomplete subprocess/thread attribution → broaden;
* changed data file → run tests observed opening it;
* stale graph → recollect or run the broader scope;
* dynamic collector detected → canonical collection only.

“Receipts” are not a magic proof. They are a transparent safety policy.

---

# The correct build order

## Milestone 1: semantic oracle and benchmark corpus

Before optimizing, create a differential harness that runs ordinary pytest and your runtime and compares:

* collected node IDs;
* item count and order;
* setup/call/teardown outcomes;
* skip and xfail reasons;
* exit codes;
* fixture setup counts;
* captured output;
* JUnit records.

Include repositories using Django, asyncio, Hypothesis, generated parameters, custom collectors and unusual `conftest.py` layouts.

This compatibility corpus is part of the project, not merely internal testing.

## Milestone 2: recorder, graph and shadow planner

Use real pytest in fresh processes.

Implement:

* canonical collection snapshots;
* function/file-level dynamic dependencies;
* static import supplementation;
* `plan`;
* `explain`;
* structured event output.

Initially, do not skip anything. Predict the impacted set, run it first, then run the remainder and measure what the predictor missed.

This is how you produce trustworthy empirical evidence.

## Milestone 3: conservative selection

Enable actual skipping locally only after shadow data exists.

Keep:

* an always-run policy;
* broad fallbacks;
* full-suite verification mode;
* graph freshness;
* missed-failure telemetry stored locally;
* simple exportable reports.

This is where you outperform testmon: not merely through a different algorithm, but through visibility and conservative invalidation.

## Milestone 4: disposable warm generations

Add the daemon only after you have exact session fingerprints and invalidation tests.

Start with:

* warm pytest and plugin bootstrap;
* no fixture persistence across invocations;
* fresh child per run;
* Linux-first fork/forkserver experiments;
* automatic fallback to a fresh process.

Then benchmark separately:

```text
cold startup
warm startup
canonical collection
targeted module collection
single-test execution
selected-suite execution
full-suite execution
```

Do not multiply independent microbenchmark ratios.

## Milestone 5: static collection fast path

Once canonical collection data exists, add static collection as an acceleration cache.

For every module:

```text
static result
vs.
canonical pytest result
```

Only use the fast path where parity has been established and the relevant fingerprint is unchanged.

## Milestone 6: external-resource isolation

Add explicit adapters for:

* Postgres;
* Redis;
* port allocation;
* test servers;
* temporary filesystems.

This is more valuable for parallel agents than generic forked-memory snapshots.

## Milestone 7: opt-in result caching

Cache only tests that are:

* declared hermetic;
* observed to have a complete resource closure;
* deterministic across verification runs;
* executed under an identical environment fingerprint.

Everything else remains selection plus execution.

---

# What the first release should demonstrate

The compelling OSS demo is not:

> “Written in Rust.”

It is:

```text
Repository: large Django application
Tests: 12,418

Plain pytest, one targeted test:
  collection/startup: 14.8s
  execution: 0.3s

Incremental runtime, warm:
  planning: 18ms
  targeted canonical collection: 210ms
  execution: 0.3s

Changed function:
  app/billing/retries.py::retry_charge

Selected:
  23 tests
Broadened:
  7 tests due to shared fixture
Forced:
  4 unmapped tests

Every selection is explainable.
A full shadow run found zero additional failures.
```

That is technical, measurable and credible.

“Six minutes to three seconds” may happen when one tiny change affects a tiny subset, but it should be an observed repository result, never the default promise. A reduction from 360 seconds to 3 seconds is 120× overall; that generally requires skipping nearly all test bodies, not merely improving startup and collection.

---

# The revised answer to “what are we building?”

I would replace the current explanation with this:

> We are not reimplementing pytest. We are building an incremental runtime for it. Real pytest remains the authority for plugin loading, canonical collection, fixtures and test execution. A Rust daemon manages disposable warm workers, records code and resource dependencies, plans affected tests and schedules execution. Static analysis provides a fast speculative index, but uncertain or dynamic cases automatically delegate to canonical pytest. Every skipped or selected test has an auditable reason, and external services such as Postgres use explicit isolation adapters rather than pretending process fork snapshots them.

And the one-line thesis should become:

> **Speed is the demonstration; safe incremental execution is the technical contribution; agents are the highest-frequency client.**

That is strong enough to justify building open source before deciding what company, if any, sits on top of it.

[1]: https://pydevtools.com/blog/charlie-marsh-test-set-interview/ "Charlie Marsh on uv, Agents, and Open Source | pydevtools"
[2]: https://docs.pytest.org/en/stable/how-to/writing_hook_functions.html?utm_source=chatgpt.com "Writing hook functions - pytest documentation"
[3]: https://github.com/hughhan1/rtest "GitHub - hughhan1/rtest: Python test runner built in Rust · GitHub"
[4]: https://github.com/neul-labs/rpytest "GitHub - neul-labs/rpytest: Run your pytest suite faster. Change nothing. · GitHub"
[5]: https://docs.neullabs.com/rpytest/reference/compatibility/ "Compatibility - rpytest"
[6]: https://docs.python.org/3/library/multiprocessing.html?utm_source=chatgpt.com "multiprocessing — Process-based parallelism — Python 3.14.6 documentation"
[7]: https://arxiv.org/html/2603.17973v1 "TDAD: Test-Driven Agentic Development – Reducing Code Regressions in AI Coding Agents via Graph-Based Impact Analysis"
[8]: https://coverage.readthedocs.io/en/latest/config.html "Configuration reference — Coverage.py 7.15.0 documentation"
[9]: https://pytest-xdist.readthedocs.io/en/stable/distribution.html?utm_source=chatgpt.com "Running tests across multiple CPUs — pytest-xdist documentation"
[10]: https://github.com/tarpas/pytest-testmon/ "GitHub - tarpas/pytest-testmon: Selects tests affected by changed files. Executes the right tests first. Continuous test runner when used with pytest-watch. · GitHub"
[11]: https://openai.com/index/openai-to-acquire-astral/?utm_source=chatgpt.com "OpenAI to acquire Astral"
