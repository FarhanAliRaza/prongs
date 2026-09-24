# Can we sell what Anthropic built? Market read, September 2026

> Question (2026-09-24): if we build a sellable version of Anthropic's test-impact-analysis service ([[anthropic-tia-scaling]]), how, and is there demand? Short answer: demand for the **outcome** — CI that stays fast, cheap and trustworthy under agent load — is now well-evidenced and compounding, and three incumbents repositioned around it in the last six months. Demand for TIA as a **standalone SKU** has never materialised: every pure play was absorbed. The sellable thing is a hosted **test-evidence service** (result history + dependency map + flake state + receipts) that an OSS agent-native runner feeds and that agents, CI and merge queues query. Sell it per committer, like everyone else does, to the AI-native mid-market that cannot build Anthropic's system in-house.

## 1. Demand: what the evidence says

### 1a. The load is real and compounding

- Anthropic: 25x CI jobs in six months, tests 10x on flat headcount, 8x code per engineer, 80% Claude-authored ([article](https://claude.com/blog/agentic-coding-is-straining-ci-heres-how-we-scaled-test-impact-analysis-at-anthropic)).
- GitHub (secondary reporting of GitHub's own figures): Actions went from 500M minutes/week (2023) to 1B/week (2025) to **2.1B minutes in a single week in early 2026**; PRs opened by AI agents went from ~4M (Sep 2025) to **>17M (Mar 2026)**; ~275M commits/week per COO Kyle Daigle ([dev.to summary](https://dev.to/kalemi/the-2026-github-actions-reset-cheaper-runners-stricter-security-and-smarter-pipelines-1mh6), [danilchenko.dev](https://www.danilchenko.dev/posts/2026-04-11-github-ai-agents-pull-requests/), [Latent Space interview](https://www.latent.space/p/github)). GitHub cut hosted-runner prices up to 39% on 2026-01-01 — the platform is feeling it too.
- Blacksmith (fast GitHub Actions runners): CI jobs on its platform growing **5–10% week over week since January 2026**; $10M Series A from GV, then a **$45M Series B from Peak XV at a $550M valuation** (raised March, announced 2026-08-12), headline verbatim: "as AI-generated code drives demand for faster code validation" ([Yahoo Finance](https://sg.finance.yahoo.com/news/blacksmith-raises-45m-series-b-110700020.html), [BigDATAwire](https://www.hpcwire.com/bigdatawire/this-just-in/blacksmith-raises-45m-series-b-as-ai-generated-code-drives-demand-for-faster-code-validation/)). Depot: $10M Series A, March 2026. The "make CI faster" neighbours are getting funded on exactly this thesis.
- GitLab research: 85% of respondents say AI moved the bottleneck from writing code to reviewing and validating it; Qodo raised $70M for code verification; Factory raised $200M at $5B on 2026-09-15 — the agent vendors are scaling the load ([codenote roundup](https://codenote.net/en/posts/ai-software-testing-startups-2026/), [SitePoint](https://www.sitepoint.com/ai-agent-testing-automation-developer-workflows-for-2026/)).
- **Counter-signal, take it seriously.** Mergify's *State of Merge Queues 2026* (2026-07-27; 200k+ merges, 477 teams): autonomous agents authoring whole PRs are "a rounding error, a few hundred merges out of 153,000"; AI-assisted PRs broke main *less* often (1.9% vs 4.4%); AI detection is a floor ([report](https://mergify.com/reports/state-of-merge-queues-2026)). Read: the 25x curve is real inside AI-native orgs and not yet at the median team. The buyer today is the AI-native org — which is also the org most able to build in-house (Anthropic, Meta, Google, Uber all did). The median team arrives 12–24 months later.

### 1b. People pay for this outcome today — through incumbents

| Vendor | Mechanism | Pricing | Notes |
|---|---|---|---|
| Datadog Test Optimization | **per-test code coverage** → Bloom filter (0.04% FP, can only run *more* tests); never-skip rules (tracked files, unskippable tests, `ITR:NoSkip`); limits: 100 commits, 5k changed files, tests covering 16k+ files never skipped. Python supported. ([docs](https://docs.datadoghq.com/tests/test_impact_analysis/)) | per active committer (3+ commits/month); TIA + flaky detection included; no public price (sibling CI Pipeline Visibility: $8/committer) ([pricing](https://docs.datadoghq.com/account_management/billing/pricing/)) | The closest analog to our map — sold as a feature of CI observability, via CI upload, one language of seven. |
| Gradle Develocity Predictive Test Selection | ML on result history; "up to 70%"; Netflix >10 min → 1–2 min | per-user annual subscription, no list price ([pricing](https://develocity.ai/pricing/), [product](https://gradle.com/develocity/product/predictive-test-selection/)) | JVM only. Runs in *simulation* first — Spring, JUnit, Micronaut are on simulations. |
| Launchable → CloudBees | ML on history + file paths; acquired Aug 2024, relaunched as **CloudBees Smart Tests**, GA 2026-04-02, positioned verbatim on "the surge of AI-generated code flooding CI pipelines"; claims 54 min → 4 min ([release](https://www.globenewswire.com/news-release/2026/04/02/3267658/0/en/cloudbees-smart-tests-brings-control-to-the-surge-of-ai-generated-code-flooding-ci-pipelines.html)) | enterprise | The best-funded pure play could not stand alone; the AI-surge framing is now its headline. |
| SeaLights → Tricentis | agents map code→tests + ML; "50–90%" cycle cut; ~$150M acquisition (2024) ([Ctech](https://www.calcalistech.com/ctechnews/article/r1l1vwh00r)) | annual, priced on code volume / repos, no self-serve ([Merito](https://www.merito.com/vendors/tricentis/sealights)) | Enterprise QA; proves TIA carries a nine-figure price *inside* a platform. |
| Codecov Automated Test Selection | pytest coverage contexts; **Python-only beta since Oct 2023, still beta** ([Sentry changelog](https://changelog.getsentry.com/announcements/codecov-automated-test-selection)) | Codecov plans | Codecov went Sentry → **Harness (2026-06-02)**, framed for "AI-accelerated engineering teams" ([PR Newswire](https://www.prnewswire.com/news-releases/harness-acquires-codecov-from-sentry-to-strengthen-software-delivery-governance-in-the-ai-era-302787720.html)). |

Adjacent price anchors: Trunk flaky tests $15–40/dev/month, $25M raised, ~62 people ([Tracxn](https://tracxn.com/d/companies/trunk-technologies/__eTn2ByDGahPgPCZbH7iwy1Iz1ePeNWDDUeigxELOHJA)); Aviator merge queue $20/dev (Team) – $40/dev (Scale) ([Graphite guide](https://graphite.com/guides/merge-queue-tools-options)); Nx Cloud $19/active contributor/month, Powerpack $26/seat, ~$24.6M raised ([nx.dev/pricing](https://nx.dev/pricing), [Tracxn](https://tracxn.com/d/companies/nx/__cfF7JP9BbHb_766jljpg0C5DGwtz3B--IR83a7V1bFU)); Buildkite Test Engine bills managed tests at P90 ([docs](https://buildkite.com/docs/test-engine/usage-and-billing)).

**The pattern:** five vendors sell TIA as a feature inside a platform; every pure play was acquired (Launchable, SeaLights, Codecov twice, YourBase 2022) and Toolchain Labs — the Python build-system company — wound down in June 2023. Nobody has made TIA a standalone company at scale. Buyers pay $15–40/dev/month for CI health (flakes, queues, caching) and TIA rides inside.

### 1c. The Python-specific gap

- pytest-xdist: ~148M downloads/month ([pypistats](https://pypistats.org/packages/pytest-xdist)). testmon remains a small fraction (July measurement ~3%; not re-measured this month). The only Python-native hosted TIA (Codecov ATS) has been "beta" for three years. Datadog/CloudBees/SeaLights treat Python as one language of many, ingest via CI upload, expose nothing to the agent before the run, and have no warm runtime. Nothing is agent-loop-native.
- Astral/OpenAI: announced 2026-03-19, still described as pending in available sources; the Astral team goes into Codex with a verification mandate ([OpenAI](https://openai.com/index/openai-to-acquire-astral/), [Simon Willison](https://simonwillison.net/2026/mar/19/openai-acquiring-astral/)). The overhang from [[hypothesis]] is unchanged.

### 1d. Reception of the article

Follow-on commentary exists ([Zhimin Zhan](https://agileway.substack.com/p/reflections-on-anthropics-new-article), [Agentic Ready](https://www.getreadyforagents.com/news/anthropic-ci-cd-agentic-strain/), a Japanese translation); no large Hacker News thread surfaced in search. The article itself predicts horizontally scaled test selection becoming standard.

**Verdict on demand.** Yes for the outcome: it is the fastest-growing cost line in AI-native engineering, and CloudBees (Apr), Harness (Jun) and Blacksmith (Aug) all repositioned around it in 2026. No for "TIA as a SKU": buyers pay for it inside CI observability, merge queues or build platforms, per committer. So sell a platform-shaped thing with selection inside, not selection.

## 2. What the sellable system is

Anthropic's service, generalised: a **test-evidence service**.

- **Ingest (their listener).** Every test run — CI job, local run, agent sandbox — posts structured observations from *inside* the run via our plugin: outcome, duration, per-test dependency set, flake signals, environment fingerprint. Journal + roll-up exactly as the article describes; multi-writer from day one.
- **Evidence store (their per-test history).** Per repo and generation: result history, dependency map, flake state, freshness.
- **Selector API.** Diff / PR / tree hash → plan with receipts (selected, broadened, forced, skipped-with-reason). Deterministic, conservative, freshness-aware. A **shadow mode** that predicts but runs everything and reports what it would have skipped and whether anything was missed — testmon's no-deselection mode, Develocity's simulations — is how trust is earned before a single test is skipped.
- **Consumers.** (1) the OSS runner in the agent loop (`fastest affected|run`), (2) a CI step / GitHub check, (3) merge queues — Mergify, Graphite, Aviator are the natural distribution because they already own the "what runs before merge" decision, (4) an MCP server so any agent can ask "what does this diff affect?" before running anything.
- **Ops as a feature.** The in==out invariant, lag metrics, and a Claude-operated watch loop: the article's Claude session pinging at 50k jobs of lag is the product demo for "your evidence service watches itself".

What makes it different from Datadog/CloudBees/SeaLights: evidence comes from inside the runtime (function-level `sys.monitoring` deps, warm generations), not uploaded coverage or JUnit; the agent is the first-class client — selection exposed *before* the run, with receipts and token-budgeted output; local-first, works with no CI at all, which is where agent loops actually run; Python-native depth (Django DB fixtures, conftest scopes, import-time execution) generalists cannot match. The moat is the per-repo evidence database that every run deepens, plus the shadow-mode miss telemetry that proves it — Nx Cloud's moat is the graph, Datadog's is the history; ours is both.

## 3. How to build it

0. **Prerequisite:** the PoC gaps in [[anthropic-tia-scaling]] — journal + roll-up schema, provenance, map fed by selected runs. Without them the service has nothing to ingest.
1. **OSS runner as the wedge** (current plan, [[hypothesis]] build order): selection with receipts + warm daemon + agent JSON, for pytest. Distribution: `uvx fastest` inside Claude Code / Codex / Cursor sandboxes, an MCP server, a GitHub Action. Free forever.
2. **Hosted evidence service v0**, single-tenant, one repo: listener / roll-up / selector behind an API key; CI step uploads observations; agents and CI query plans. Shadow mode by default. The output that matters is a number: *zero missed failures over N runs on your repo* — that is the sales asset.
3. **Flake state and quarantine** — a query on the per-test history once it exists, and the feature with the clearest proven willingness to pay (Trunk).
4. **Merge-queue and CI integrations; team pricing.**
5. **Shared map across worktrees and agents, then content-addressed result cache** — the Nx Cloud endgame, only after hermeticity evidence.

**Decision gates** (price the bet honestly):

- Shadow-mode miss rate on three design-partner repos over 90 days: **zero** on status changes the policy claims to cover; median selected fraction **<30%**.
- Within six months of hosted v0: **≥3 design partners running un-shadowed selection in CI, ≥1 paying ≥$20/committer**.
- Agent-loop adoption: runs/day through agent sandboxes, because that is the distribution thesis.

## 4. Pricing and who buys

- Per active committer, **$20–40/month** (the Trunk / Aviator / Nx band); free for OSS and ≤5 committers; enterprise self-hosted tier priced per repo (the SeaLights model) for the AI-native orgs that want it in their VPC.
- Buyer: platform / DevEx lead at a **50–500 engineer company with a Python monolith** (Django, FastAPI, ML), an agent fleet rolling out, and CI spend plus queue time now a budget line. Not the Anthropic-sized org (they build it) and not the five-person team (free tier).
- The alternative channel [[hypothesis]] already flagged: **sell to the people running the sandboxes** — E2B / Modal / Runloop, Blacksmith (which is now building its own coding agent, codesmith), Factory. They need verification throughput per sandbox; one contract is fleet-wide distribution.

## 5. Risks

- **Incumbent bundling.** Datadog ships per-test-coverage TIA with Python support today. Counter: agent-first, local-first, receipts, runtime depth — and being the thing that runs *inside* the sandbox where Datadog is not.
- **The Astral/Codex overhang** — unchanged.
- **Timing.** Mergify's data says the median team is not at 25x yet. The market may be small in 2026 and large in 2028; runway must span that.
- **Trust.** One wrong skip loses a customer; shadow mode first, always, and the miss telemetry is public per repo.
- **The pure-play graveyard.** Be a platform feature set — evidence, flakes, selection, cache — not "TIA".

## Recommendation

Build the OSS runner as planned. Build the hosted evidence service only as the journal the runner already needs — so it is nearly free to stand up — run it in shadow mode with three design partners, and decide on the company at the gates above. Do not pitch "test impact analysis"; pitch **the verification layer for Python coding agents**, with the evidence service as the paid layer. That is the Nx Cloud shape, in the one ecosystem where nobody has built it, at the moment the load curve turned vertical.
