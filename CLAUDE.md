# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

sylabis is a single-learner "learning agent" CLI (`sy` / `sylabis`). Tell it a topic;
it **compiles** a course from primary sources, **teaches** milestone by milestone,
**grades** the real artifacts you build, **adapts** the path when you struggle or excel,
and **connects** everything you verify into one cross-course knowledge map. Pure Python
3.10+, four runtime deps (`anthropic`, `pyyaml`, `python-dotenv`, `httpx`); the web app
and MCP server are stdlib-only.

## Commands

```bash
pip install -e .                 # dev install; gives you `sy` and `sylabis` from the checkout
python -m tests.run_all          # the entire test suite (exit 0 only if every case passes)
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
sy init                                   # once: saves the key to $SYLABIS_HOME/.env (0600)
sy                                        # talk — the streaming agent drives the loop
sy web [--port 8787]                      # the same journey in the browser (127.0.0.1 only)
sy learn "topic" [--hours N --hardware .. --prior ..]
sy next / sy submit / sy journey / sy attach SOURCE
sy publish / sy sync / sy share           # GitHub: repo per bundle, CI grades pushes
python -m sylabis.cli serve [COURSE_DIR]  # MCP stdio server (journey-wide without a dir)
```

## Architecture — the big picture

**Everything is on disk; there is no database.** A journey is `$SYLABIS_HOME`
(default `~/sylabis`); a course is a directory of YAML + OKF-frontmatter markdown.
`journey.py` *reads* the bundles — it never copies or caches them — so it can never
disagree with the courses it describes. Attached courses are symlinks (local path) or
clones (git URL), so cross-repo curricula count identically to local ones.

Three doors, one core: the **terminal agent** (`agent.py` + `console.py`), the **web
Reading Room** (`web.py`), and any **MCP client** (`mcp_server.py`) all drive the exact
same journey-scoped tool registry in `tools.py`. Add a capability once as a tool and all
three surfaces gain it.

Data flow, source → screen:

- **`config.py`** — key + model config. `load_env()` loads `.env` from cwd then
  `$SYLABIS_HOME/.env` (real env always wins); `save_key()` is what `sy init` writes
  with; `model()` honors `$SYLABIS_MODEL` at call time. Never store a key elsewhere.
- **`gitio.py`** — bundles as git repos: `ensure_repo` (from birth, at compile),
  `commit_all` (skip-when-clean — the idempotency contract), `publish`/`sync` (gh CLI
  or manual recipe; ff-only pulls; the `course.published`/`course.synced` events are
  committed *inside* the pushed history so re-runs converge). Git is an enhancement:
  every function no-ops without it, and **git never blocks or alters a grade**.
  Auto-commit happens ONLY on the submit paths (`cli._submit`, `tools._t_submit_work`)
  — never in `sylabis grade` plumbing, which CI runs and which commits its own state.
- **`llm.py`** — the *only* place model calls happen. `.call()` for
  one-shot compiler/grader stages, `.chat()` for the agent's tool-use loop. `.chat()`
  has **no mock mode by design** (you test the tools, not the conversation); `.call()`
  fully mocks. `parse_json()` strips the markdown fences LLMs add no matter what.
  A missing key raises a friendly SystemExit here (surfaced as ToolError by tools.py);
  `validate_key()` is the only intentionally-networked function — only `sy init` calls it.
- **`prompts.py`** — all 8 system prompts as module constants. **Treat these as code:**
  the compiler/grader stages *are* their prompts; changing one changes pipeline behavior.
  Compiler prompts demand `Output ONLY JSON`; that contract is enforced by `parse_json`.
- **`compiler.py`** — `compile_course()`: intake → harvest → sequence → emit → self-test.
  Intake's viability verdict can `decline` (raises `SystemExit`, refuses to compile).
  `self_test()` gates shipping: structural + OKF-conformance checks must pass.
  `compile_remedial()` injects a 1-hour reflection-only micro-module.
- **`verify.py`** — harvest source-locator verification (arXiv API / doi.org / HEAD).
  **Flags-and-continues — verification never blocks a compile.**
- **`okf.py`** — owns **ALL** frontmatter/markdown production for a bundle. No other
  module writes frontmatter. The bundle on disk *is* the credential; `okf.yaml`
  inventories every doc (with sha256 — tamper-evident, not signed) so a claim
  verifies without an external authority. Two deterministic doc types carry the
  shareable story: `grade-report` (`portfolio/reports/<mid>.md`, emitted on every
  grade, pass or fail, values copied **verbatim** from the result — the emitter
  formats, never computes) and `readme` (root `README.md`, the landing page GitHub
  renders; regenerated on compile, grade, and remedial injection). Zero LLM calls,
  zero fixtures needed.
- **`grader.py`** — `grade()`: **Tier 1** deterministic structural → **Tier 2** claim
  audit → **Tier 3** rubric → **explain-back**. Tiers short-circuit: a Tier 1 block
  never runs Tier 2. Tier 3 has two modes: `executable` runs learner scripts
  (`SCRIPT_TIMEOUT=300s`, crashes/garbage/out-of-range all fail loudly with feedback),
  `exemplar_rubric` is the calibrated LLM judge and stays **OFF until a human seeds
  exemplars**. Explain-back always runs last and can *cap* an otherwise-passing grade.
- **`path_engine.py`** — `decide()` (pure: grade result → decisions) and `actuate()`
  (the *only* place decisions mutate state: unlock sidequests, inject remedials).
  **Deliberately a legible rules table, not a model** — if you can't explain an unlock
  to a learner in one sentence, it doesn't belong here. `actuate()` must be idempotent.
- **`events.py`** — append-only `events.jsonl` (`schema: 1`), written from day one as
  the pathway-graph seed. Every event carries `id, ts, type, payload`.
- **`journey.py`** — connected curriculum: `next_steps`/`submittable`, `knowledge()`
  (verified concepts + which course/artifact/grade proved them), `prior_knowledge()`
  (feeds the next compile as assumed knowledge), `attach()`, `emit_map()` → `knowledge.md`.

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
- **`actuate()` is idempotent and the sole state mutator** in the path engine; re-grading
  or re-running must not stack remedials or double-emit events.
- **Auto-commit only on submit; the `grade` plumbing never commits** — CI runs
  `sylabis grade` and grade.yml commits its own state; a commit inside the plumbing
  double-commits every CI run. `gitio.commit_all` skips when clean; publish/sync
  events ride inside the pushed commit so repeated runs converge to no-ops.
- **Grade reports and READMEs present recorded grades verbatim** — never synthesize,
  re-derive, or embellish a number; ungraded milestones are shown, not hidden.
- **grader.py contains no git and no publish logic**; `okf.emit_bundle_manifest` is
  emitted LAST in `grader._write` so okf.yaml never goes stale after a grade.
- **Tool routes refuse path traversal** (`../`, absolute, nonexistent) — see
  `tools.ToolError` and the web `doc` route; keep new routes doing the same.
- **`okf.py` is the only frontmatter writer**; route all doc production through it.

## Git & workflow

- The MCP-served per-course tool scope (`course_overview`, `submit_artifact`, …) is a
  distinct legacy surface kept working for existing bundles — the journey scope
  (`journey`, `start_course`, `submit_work`, …) is the primary one. Changing tool
  behavior may mean touching both registries.
- Compiled bundles ship `.github/workflows/grade.yml`: pushing `artifact.md`/`reflection.md`
  triggers grading, feedback lands as a commit comment (push) or PR comment (PR).
- Journey state (`/courses/`) and `.env` are gitignored; never commit a learner's journey.
