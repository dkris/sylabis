# ADR 001 — Agent architecture: stay hand-rolled, adopt no orchestration framework

- **Status:** Accepted (Primetime plan, Phase 1 WS4, decision D1)
- **Date:** 2026-07
- **Deciders:** solo founder
- **Scope:** the whole model-calling surface — compile pipeline
  (`compiler.py`), grader (`grader.py`), path engine (`path_engine.py`),
  tutor loop (`agent.py` over `tools.py`), and the `llm.py` choke point.

## Decision

sylabis adopts **no agent-orchestration framework** in Phase 1. The
hand-rolled architecture stays: one model gate (`llm.py`), one tool
registry (`tools.py`), one ~small streaming loop (`agent.py`). Two
targeted upgrades land instead — **Anthropic structured outputs** for
the one-shot stages and an **orchestrator-workers harvest** inside
`compiler.py` — both entirely within existing modules.

## Context and rationale

In Anthropic's own workflows-vs-agents taxonomy, **sylabis is workflows,
not agents**. Framework value concentrates in dynamic, open-ended agent
graphs; sylabis has exactly one of those, and it is tiny:

- **The compile pipeline is prompt chaining with typed JSON handoffs.**
  `compile_course()` runs intake → harvest → sequence → lesson×N → emit
  → self-test as discrete `LLM.call()`/`call_json()` stages, each output
  checkpointed to `.compile/<stage>.json` and validated against a
  per-stage schema. There is no dynamic control flow for a graph engine
  to add value to.
- **The grader is an evaluator-optimizer with *load-bearing sequential
  short-circuits*.** Tier 1 (structural) blocks before Tier 2 (claim
  audit) runs; Tier 2 overclaiming blocks before Tier 3 quality
  judgment; explain-back runs last and can cap a passing base score.
  Running these as parallel agents would *break the product* — the
  ordering is the calibration. A framework's parallelism is an
  anti-feature here.
- **`path_engine.py` is deliberately a legible rules table**, not a
  model, and must never become model-mediated: if an unlock can't be
  explained to a learner in one sentence, it doesn't belong in the
  table. There is nothing to orchestrate.
- **Only the tutor loop is a true agent** — and it is roughly twenty
  lines of loop over the `tools.py` registry: stream a turn, execute
  tool_use blocks, append results, repeat until `end_turn`. A framework
  would replace those lines with a dependency, not remove complexity.

Three assets any framework adoption would have to preserve, and most
would degrade:

1. **The `--mock` fixture contract** — every `LLM.call()` routes through
   `fixtures/<stage>.json`, which is what makes the whole suite offline
   and deterministic. Frameworks that own the model call own the seam.
2. **One registry, three surfaces** — terminal, web, and MCP drive the
   same `tools.py`. Framework-native tool definitions would fork this.
3. **The no-database state model** — the on-disk bundle plus
   `events.jsonl` *is* the durable state and the audit log.

## Frameworks surveyed

| Framework | Verdict | Why |
|---|---|---|
| **PydanticAI** | Least-bad; not adopted | Closest philosophical fit (typed outputs, thin abstractions, model-agnostic). Still replaces the `LLM.call` mock seam and adds a dependency for what `call_json` + `STAGE_SCHEMAS` already do in stdlib. First candidate if a rewrite is ever forced (see re-entry criteria). |
| **LangGraph** | Rejected | Its headline feature — graph checkpointing/resume — duplicates what `.compile/<stage>.json` + the bundle + `events.jsonl` already are. Two sources of durable truth is how state models rot. |
| **Claude Agent SDK** | Rejected | Its default filesystem-writing tools sit directly against the product's core invariant: **the guide never writes the learner's artifact**. Stripping the defaults back out leaves little SDK left. |
| **CrewAI** | Rejected | Opinionated role/crew abstractions with no mapping onto a fixed five-stage pipeline; heavy dependency surface. |
| **AG2 (AutoGen fork)** | Rejected | Maintenance-lineage risk after the project split; unclear long-term stewardship for a load-bearing dependency. |
| **smolagents** | Rejected | Code-execution-centric agent posture (model-written code as the primary tool call) is the opposite of this product's execution-containment stance. |

## Named Phase-2 re-entry criteria

This is a decision, not a dodge — the conditions that reopen it are
named now:

- **Hosted resumable long-running compiles or per-tenant durable
  execution** (the Phase-2 hosted product) → adopt **PydanticAI +
  Temporal/Hatchet first**, LangGraph second. Durable-execution engines
  match the staged pipeline shape; the graph framework is the fallback,
  not the default.
- **If Anthropic should host the loop and the sandbox** → evaluate
  **Anthropic Managed Agents** at that point, against the same three
  assets above.

## Tool Runner: declined

The Anthropic beta Tool Runner would shrink `agent.py`'s loop, but the
loop is small, tested via `_StubModel` at exactly the right seam, and
beta-status API churn isn't worth trading a working, tested loop for.
**Revisit at GA.**

## Structured outputs (WS4.1) — recorded

`LLM.call()` now sends each JSON-emitting stage's JSON Schema (from
`prompts.STAGE_OUTPUT_SCHEMAS` — the prompt+schema pair *is* the stage)
through the API's `output_config.format` mechanism (`type:
"json_schema"`, anthropic SDK ≥ 0.117), so on live calls a schema
violation is impossible by construction on the request side. The stdlib
required-keys validation (`validate_stage` / `STAGE_SCHEMAS`) and the
one-round repair loop in `call_json` are **retained as fallback**: they
are what mock mode validates against, and they cover any live response
the API constraint could not (SDK/API drift). The mock seam stays at
`.call()` — every fixture is unchanged.

## Measured results (WS4.2 harvest + emission)

Recorded per the WS4 acceptance criteria (before → after):

- **Mock 2-milestone compile (whole pipeline, offline):**
  0.047–0.076 s → 0.047–0.049 s. Mock compiles were never the
  bottleneck; the point is the concurrency machinery adds no overhead
  and the suite stays deterministic.
- **Lesson phase:** serial `N × T` → `ceil(N / W) × T` at
  `W = LESSON_WORKERS = 3`. Measured with 9 simulated 0.2 s lessons:
  **1.81 s → 0.61 s (2.97×)**. On live compiles, where a lesson call is
  tens of seconds, this is the difference that makes compile latency
  sublinear in course size.
- **Source verification:** the old serial loop cost
  `0.5 s × N + Σ(latencies)` (a politeness sleep per source); the
  concurrent path costs the **max over batches of 8**
  (`verify.MAX_CONCURRENCY` — the bound is the politeness mechanism,
  and one compile's sources spread across many hosts). Results are
  cached in `.compile/verify_cache.json` keyed by raw locator, so
  resumes and re-compiles never re-hit a host for a locator already
  checked (transient network errors are deliberately not cached).

## Consequences

- Reliability features (retries, schemas, checkpointing, cost events,
  model routing) all landed as retrofits inside `llm.py`/`compiler.py` —
  the single-chokepoint design paid off exactly as predicted.
- A **grader-isolation invariant** is documented in CLAUDE.md alongside
  this ADR: Tier-3 judgment always runs in a fresh context, never
  sharing the compile or tutor conversation (the independent-judge
  pattern). It is an invariant to preserve, not a service to build.
- Debate/ensemble grading for high-stakes grades is named Phase-2 work,
  gated on the exemplar-calibration path (which correctly stays off
  until human-seeded).
