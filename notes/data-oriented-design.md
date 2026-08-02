# Data-oriented design lessons for the Rust core

> Source: Arshad Yaseen, "Engineering high-performance parsers" (arshad.fyi/writings/engineering-high-performance-parsers, read 2026-07-13) — a write-up of the Yuku parser. Thesis: parser performance lives in **memory representation** (flat arrays + integer indices, not pointer trees), and the user-facing cost is usually the **boundary crossing** (handing results to the consumer), not the computation. Both map directly onto [[hypothesis]]; this note captures what to adopt and when.

## Two invariants to adopt NOW (cost nothing, prevent drift)

1. **Output cost scales with what the agent reads, never with suite size.** Yuku's wire format's cost "scales with what the consumer reads rather than with the size of the AST." Our agent JSON already behaves this way by accident (2,201 B vs pytest's 12,871 B on the same failures; passes summarized, failures grouped by root cause). Make it a stated design rule: no per-test output in the default schema, ever — a 100k-test suite and a 100-test suite with the same 3 failures must produce the same-sized result.
2. **Nothing gets added to the fork-path hot loop.** His control-plane/data-plane split: setup may be complex and slow (our 36s wagtail warm-up — migrations, shims, pinned connections); the hot path must stay minimal (our ~0.4s pytest-session floor per fork, and eventually per-test fork cost). Any feature that adds cost to the fork path is architecturally suspect — push it into warm-up or the parent. This is the budget discipline for [[poc-results]]'s "fork from a post-collection template" work.

## The design doc for the Rust dependency DB (adopt at scale, not before)

Current `mapdb.py` is the textbook design: normalized SQLite, string keys, one JOIN query per changed function in `select.py`. Fine at wagtail scale (921k links, 24MB, replaced nightly). The Yuku-style representation, which is also what [[verdict]] sketched independently:

- **Intern everything to u32s**: test IDs, file IDs, (file, qualname) code units. Strings live once in an interned table; the graph is integers only.
- **Reverse index as flat bitmaps**: `code_unit_id -> bitmap(test_ids)` (roaring or plain). Diff planning = a few bitmap unions + policy broadening — microseconds, independent of suite size.
- **The on-disk format IS the in-memory format**: position-independent (indices, not pointers), so the file can be mmap'd by the Rust daemon and handed across the Rust↔Python boundary with zero serialization. This is his single biggest trick and it kills our future IPC-marshalling cost before it exists.
- **Struct-of-arrays**: durations, statuses, and dependency bitmaps in separate columns, so the scheduler (reads durations) and the planner (reads bitmaps) don't drag each other's data through cache.

Trigger for actually doing this: selection planning or map load appears in the per-run profile, or a target repo pushes past ~10⁷ links. Not before — SQLite is nowhere near limiting at PoC scale, and correctness work (fork-per-test, subprocess attribution, new-test handling) outranks it.

## Validated already, worth naming

- **"Defer work the input doesn't require" is our biggest measured win.** Plugin v1 re-armed monitoring per test: 31% overhead (kill-criterion territory). v2 classifies each code object once and permanently DISABLEs non-project code: 6–13%, 1.1% on wagtail. Same principle as his ASCII fast path. Next application if recorder overhead ever matters: intern code objects to ints, per-test bitset instead of a Python set.
- **His conformance methodology = our parity oracle.** Yuku validated its language boundary against 50k+ differential conformance cases. That's exactly the Phase 4 "static proposes, pytest disposes" mechanism (and [[verdict]]'s Milestone 1 corpus) — adopt the discipline at PoC size now; the corpus-as-product waits for a go decision.

## Honest scope limit

None of this touches our dominant costs. Test bodies are Python executing user code; startup is process/import weight; both are attacked by selection and the warm daemon, not by data layout. Data-oriented design matters in exactly two places for us: the planner's query path at monorepo scale, and the recorder's per-event callback. His caveat applies to Phase 4 too: Python `ast` already parses a test tree in well under a second — static collection's risk is parity, not parse speed, so the flat-AST representation is a someday-optimization, not the design driver.

## The meta-lesson

"One representational decision solves problems that look unrelated": his flat AST simultaneously fixed allocation, cache misses, serialization, and the language boundary. Our equivalent single decision is the **interned (file, function) → test-bitmap graph** — it is simultaneously the selection planner, the duration-aware scheduler's input, the content-addressed cache's dependency closure, and the receipt evidence. Get that representation right once; every feature in [[hypothesis]]'s build order is a consumer of it.
