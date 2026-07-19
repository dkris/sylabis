# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

sylabis is a single-learner "learning agent" CLI (`sy` / `sylabis`). Tell it a topic;
it **compiles** a course from primary sources, **teaches** milestone by milestone,
**grades** the real artifacts you build, **adapts** the path when you struggle or excel,
and **connects** everything you verify into one cross-course knowledge map. Pure Python
3.10+, four runtime deps (`anthropic`, `pyyaml`, `python-dotenv`, `httpx`) plus an
optional `[tui]` extra (`textual`); the web app and MCP server are stdlib-only.

## Commands

```bash
pip install -e .                 # dev install; gives you `sy` and `sylabis` from the checkout
pip install -e ".[tui]"          # + the optional Textual full-screen terminal app
python -m tests.run_all          # the entire test suite (exit 0 only if every case passes)
ruff check .                     # lint (CI runs it; config in pyproject.toml)
```

- **Tests are a single stdlib runner, not pytest.** `tests/run_all.py` collects every
  top-level `case_*`/`test_*` function and runs each in a temp dir. To run one test,
  edit the `TESTS` list at the bottom or call the function directly — there is no `-k`.
- **The suite is fully offline and deterministic**: every model response is a fixture
  from `fixtures/`, adversarial cases overlay their own fixtures on top. No API key,
  no network. Never add a test that hits the real API.
- **`--mock` is the testability contract, not a toy.** `learn`, `submit`, `compile`,
  `grade`, and the web/MCP servers all take `--mock` (or `mock=True`), which routes
  every `LLM.call()` through `fixtures/<stage>.json` instead of Claude. If you add a
  compiler/grader stage, add its fixture or every mock test breaks.

### Running the app
```bash
export ANTHROPIC_API_KEY=sk-ant-...       # or put it in .env (python-dotenv loads it)
sy                                        # talk — Textual TUI if installed, ANSI agent otherwise
sy web [--port 8787]                      # the journey in the browser (127.0.0.1, tokenized URL)
sy learn "topic" [--hours N --hardware .. --prior ..]
sy next / sy submit / sy journey / sy attach SOURCE
sy publish COURSE --to DIR                # scrub a course into a shareable template
sy paths search QUERY / sy paths get ID   # the public registry client (registry.py)
sy --version
python -m sylabis.cli serve [COURSE_DIR]  # MCP stdio server (journey-wide without a dir)
sylabis-mcp                               # same server as a console script (uvx-friendly)
```

Env switches: `SYLABIS_NO_TUI=1` forces the ANSI agent even when Textual is
installed; `SYLABIS_NO_BROWSER=1` stops `sy web` auto-opening the tokenized URL;
`SYLABIS_NO_UPDATE_CHECK=1` (or `DO_NOT_TRACK=1`) disables the once-daily cached
PyPI version notice — the only network call that isn't the model API or source
verification. No telemetry, ever, by default or otherwise.

## Architecture — the big picture

**Everything is on disk; there is no database.** A journey is `$SYLABIS_HOME`
(default `~/sylabis`); a course is a directory of YAML + OKF-frontmatter markdown.
`journey.py` *reads* the bundles — it never caches them — so it can never
disagree with the courses it describes. Attached courses are **copies** (local path)
or clones (git URL), never symlinks: grading and path actuation mutate the attached
bundle and must never corrupt the original checkout. Every attach writes a
provenance record (`.sylabis-attach.yaml`: source, pinned commit SHA, timestamp,
fingerprints of every grade that pre-dated the attach).

Three doors, one core: the **terminal agent** (`agent.py` + `console.py`, with
`tui.py` as an optional Textual view over the same loop), the **web Reading Room**
(`web.py`), and any **MCP client** (`mcp_server.py`) all drive the exact same
journey-scoped tool registry in `tools.py`. Add a capability once as a tool and all
three surfaces gain it.

Data flow, source → screen:

- **`errors.py`** — the typed `SylabisError` hierarchy (`ModelError`,
  `TruncationError`, `SchemaError`, `CompileError`, `CompileDeclined`, `GradeError`,
  `AttachError`, `SandboxError`, `PublishError`, `RegistryError`). Library code
  never raises `SystemExit`; the CLI catches `SylabisError` at the top and exits,
  the web/MCP surfaces render it.
- **`llm.py`** — the *only* place model calls happen. `STAGE_MODELS` routes stage
  families to models (Sonnet for generation, Haiku for claim-audit/explain-back;
  the old single `MODEL` constant survives only as a deprecated alias). `.call()`
  for one-shot compiler/grader stages: retries with exponential backoff on
  transient API errors, `temperature=0`, truncation detection (a `max_tokens`
  cutoff raises `TruncationError`, never a silently truncated doc), per-stage
  token/cost `llm.usage` events into `events.jsonl` when `usage_dir` is set, and
  on live calls the stage's JSON Schema goes up as a structured-outputs constraint
  (`output_config.format`). `.call_json()` adds stdlib required-keys validation
  (`validate_stage`/`STAGE_SCHEMAS`) with ONE repair round, then `SchemaError` —
  belt-and-suspenders under the API constraint, and what mock mode validates
  against. `.chat()` (the agent's tool-use loop) has **no mock mode by design**
  (you test the tools, not the conversation); `.call()` fully mocks.
  `parse_json()` strips the markdown fences LLMs add no matter what.
- **`prompts.py`** — all 8 system prompts as module constants, each with a
  machine-readable entry in `PROMPT_VERSIONS` and (for JSON stages) a JSON Schema
  in `STAGE_OUTPUT_SCHEMAS`. **Treat these as code:** the prompt+schema pair *is*
  the stage; change one, review the other, bump the version — provenance stamps in
  bundles record it.
- **`compiler.py`** — `compile_course()`: intake → harvest → sequence → lessons →
  emit → self-test. Every stage checkpoints to `<course_dir>/.compile/<stage>.json`
  (`status.json` is the shared progress contract the web surface polls); a crashed
  compile resumes from the last completed stage without re-buying model calls.
  Intake's viability verdict can decline — raises `CompileDeclined` (typed, never
  `SystemExit`). Harvest is propose → verify (concurrent, cached in
  `.compile/verify_cache.json`) → consolidate: unverifiable resolvable locators
  are DROPPED before sequencing (event-logged), with a floor — if more than half
  would drop, everything is kept-but-flagged and a `compile.warning` is emitted.
  Per-milestone lesson calls run under a bounded pool (`LESSON_WORKERS = 3`);
  emission stays serial so `okf.py` is never entered concurrently. Per-stage
  `{model, prompt_version, sylabis_version}` provenance is stamped into
  `course.yaml` meta. `self_test()` gates shipping: structural + OKF-conformance
  checks must pass. `compile_remedial()` injects a 1-hour reflection-only
  micro-module. Events record `learner_profile_hash`, never the raw profile.
- **`verify.py`** — harvest source-locator verification (batched arXiv API /
  doi.org / HEAD), concurrent under a bounded pool (`MAX_CONCURRENCY = 8` — the
  bound is the politeness mechanism), with an optional locator-keyed cache.
  **Flags-and-continues — verification never blocks a compile** (the compiler's
  consolidation step decides what to do with the flags).
- **`okf.py`** — owns **ALL** frontmatter/markdown production for a bundle. No other
  module writes frontmatter. The bundle on disk *is* the credential; `okf.yaml`
  inventories every doc **with a SHA-256 hash** (tamper-evident, not tamper-proof;
  `hash_problems()` verifies at publish/attach boundaries — a live journey bundle
  legitimately drifts between manifest emissions).
- **`grader.py`** — `grade()`: **Tier 1** deterministic structural → **Tier 2** claim
  audit → **Tier 3** rubric → **explain-back**. Tiers short-circuit: a Tier 1 block
  never runs Tier 2. Tier 3 modes: `rubric_scripts` runs bundle-declared scripts
  through `sandbox.SandboxRunner` (`SCRIPT_TIMEOUT=300s`, path-contained,
  scrubbed env; crashes/garbage/out-of-range all fail loudly), `exemplar_rubric`
  is the calibrated LLM judge and stays **OFF until a human seeds exemplars**,
  `claim_audit_derived` (the unseeded fallback) uses the raw audit pass ratio as
  the base score, and an executable checkpoint with no scripts is **`unscored`** —
  pass/fail from Tiers 1-2 + explain-back, `grade` key omitted from `grade.yaml`,
  no scored portfolio claim (never a fabricated number). Explain-back always runs
  last and can *cap* an otherwise-passing grade. LLM stages go through
  `call_json`, and each stamps `{model, prompt_version, sylabis_version}`
  provenance into `grade.yaml`. Grading an attached bundle that declares scripts
  requires recorded consent first (`sandbox.ConsentRequired`).
- **`sandbox.py`** — containment for rubric scripts: allowlisted env
  (`PATH`/`HOME`/`TMPDIR` — never the API key), bwrap/nsjail when installed *and*
  probe-verified (no network, read-only rootfs, rlimits), loud origin-naming
  warning on the `--unsandboxed`/no-sandbox fallback, and the once-per-bundle
  trust marker for the attach consent gate.
- **`path_engine.py`** — `decide()` (pure: grade result → decisions) and `actuate()`
  (the *only* place decisions mutate state: unlock sidequests, inject remedials).
  **Deliberately a legible rules table, not a model** — if you can't explain an unlock
  to a learner in one sentence, it doesn't belong here. `actuate()` must be idempotent.
  Unscored grades reach it through `grader.path_engine_view()`, which substitutes a
  neutral number so the rules table never learns about `None`.
- **`events.py`** — append-only `events.jsonl` (`schema: 1`), written from day one as
  the pathway-graph seed. Every event carries `id, ts, type, payload`. Now also the
  cost ledger (`llm.usage` per stage) and the compile audit trail
  (`harvest.dropped`, `compile.warning`).
- **`journey.py`** — connected curriculum: `next_steps`/`submittable`, `knowledge()`
  (verified concepts + evidence, each row tagged `origin: local` or
  `attached:<source>`, with `preexisting: True` on grades that shipped with an
  attached bundle), `prior_knowledge()` (feeds the next compile as assumed
  knowledge — **excludes** concepts whose only evidence is a pre-existing grade in
  an attached bundle, so a hand-edited `passed: true` never seeds a compile),
  `attach()` (copy/clone + provenance), `emit_map()` → `knowledge.md`.
- **`publish.py`** — `sy publish`, the privacy scrubber: allowlist transform into a
  fresh template (learner block/`topic_prompt`/`events.jsonl`/grades/artifacts/
  reflections/portfolio claims/dotfiles structurally excluded; the grade workflow
  is the one allowed dotpath), mandatory license (default CC-BY-4.0), then an
  all-or-nothing gate — `self_test()` + secret scan + PII audit; any failure
  raises `PublishError` and removes the template. All doc re-emission routes
  through `okf.py`. See `docs/publishing-guide.md`.
- **`registry.py`** — the `paths.json` registry client: `sy paths search` (cached
  static-index fetch) and `sy paths get` (attach at the listing's pinned SHA),
  plus the journey-page publisher behind `sy journey --publish`. The registry
  itself is serverless — see `docs/registry/`.
- **`web.py`** — the Reading Room, hardened Jupyter-style: per-session bearer
  token printed as a one-time URL and exchanged for an `HttpOnly` cookie,
  Host-header validation (DNS-rebinding defense), Origin + CSRF checks on POSTs,
  body-size caps, CSP. Compiles run on a background thread; the page polls
  `/status/<job>`, which reads the compiler's own `.compile/status.json` — the
  progress display is truthful, not animated.
- **`tui.py` / `agent.py`** — the Textual app is a *view* over the same
  `Agent.turn()` + `tools.py` registry (three panes; the submit editor is input
  capture only — no AI assist in that widget, ever). The agent itself is
  crash-proof: API errors retry with backoff and on final failure keep the session
  alive; the transcript persists to `$SYLABIS_HOME/.session.json` and trims to a
  message budget (oldest tool results first).
- **`update_check.py`** — the once-daily cached PyPI version notice; plain GET, no
  identifiers, never self-updates, honors the env guards above, silent when not a tty.

The `sy submit` UX ("no paths, no ids") is `cli._submit`: it finds the milestone whose
required files are on disk and only asks for an id when several qualify.
`_grade_and_adapt()` is the shared back half of `submit` and `grade` — grade, then let
the path engine act.

## Invariants to preserve (these are deliberate, not shortcuts)

- **The guide never writes the learner's artifact or reflection.** `submit_work` accepts
  only text the learner gave verbatim. If the work isn't the learner's, the grades — and
  the portfolio built on them — mean nothing. This is the product thesis; do not soften it.
- **Grades come only from `submit_work`/`grade`.** The agent must never predict or promise one.
- **Tier ordering and short-circuits are load-bearing** — overclaiming (Tier 2) must block
  before quality judgment (Tier 3); explain-back's cap must beat a passing base score.
- **Grader isolation**: Tier-3 judgment always runs in a fresh context — a one-shot
  call that never shares the compile or tutor conversation. The judge stays
  independent of anything that produced or coached the work.
- **`actuate()` is idempotent and the sole state mutator** in the path engine; re-grading
  or re-running must not stack remedials or double-emit events.
- **Tool routes refuse path traversal** (`../`, absolute, nonexistent) — see
  `tools.ToolError`, the web `doc` route, and the rubric-script containment in
  `grader._run_rubric_scripts`; keep new routes doing the same.
- **`okf.py` is the only frontmatter writer**; route all doc production through it.
- **Rubric scripts never see the API key.** Execution goes through
  `sandbox.SandboxRunner` with the allowlisted env; attached bundles additionally
  require the once-per-bundle consent gate before any script runs.
- **No learner data leaves the machine except through `sy publish`'s allowlist**,
  and no default telemetry, ever. Events record `learner_profile_hash`, never the
  raw profile.

## Git & workflow

- CI (`.github/workflows/ci.yml`) runs the test suite across Python 3.10–3.13 plus
  `ruff check` and gitleaks; a version tag triggers the PyPI release workflow
  (trusted publishing). Keep `CHANGELOG.md` current with anything user-visible.
- The MCP-served per-course tool scope (`course_overview`, `submit_artifact`, …) is a
  distinct legacy surface kept working for existing bundles — the journey scope
  (`journey`, `start_course`, `submit_work`, …) is the primary one. Changing tool
  behavior may mean touching both registries.
- Compiled bundles ship `.github/workflows/grade.yml`: pushing `artifact.md`/`reflection.md`
  triggers grading, feedback lands as a commit comment (push) or PR comment (PR).
- Journey state (`/courses/`) and `.env` are gitignored; never commit a learner's
  journey — and `sy publish` is the only sanctioned way learner-adjacent content
  leaves a journey.
- The agent-architecture decision (no orchestration framework, and why) is recorded
  in `docs/adr/001-agent-architecture.md`; the attach/grading/web threat model in
  `docs/threat-model.md`.
