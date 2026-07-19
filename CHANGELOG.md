# Changelog

All notable changes to sylabis are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] — Phase 1 "Primetime" (unreleased; tag `v0.2.0` to ship)

The first public release. Everything below is Phase 1 of the Primetime
plan; the theme is hardening the single-learner core and making sylabis
installable, auditable, and honest about what it does and doesn't do.

### Added
- **Packaging & distribution (WS2)**: PyPI-grade metadata (AGPL-3.0-only,
  classifiers, project URLs), version single-sourced from
  `sylabis.__version__` and surfaced as `sy --version`; `[tui]` extra for
  the Textual terminal app; `sylabis-mcp` console script so MCP client
  config is the conventional `uvx --from sylabis sylabis-mcp`; CI across
  Python 3.10–3.13 with ruff and full-history gitleaks; tag-triggered
  PyPI release via trusted publishing (no long-lived token);
  `install.sh` pinned to a release tag instead of `main`.
- **Update etiquette (WS2)**: a once-daily cached PyPI version check that
  prints a one-line notice; disabled by `SYLABIS_NO_UPDATE_CHECK` or
  `DO_NOT_TRACK`, silent in CI/tests/mock runs, and never self-updates.
  No telemetry of any kind, by default or otherwise.
- **Reliability core (WS1a)**: retries with exponential backoff and a
  typed `SylabisError` hierarchy replacing library `SystemExit`s;
  schema-checked model output with one repair round; compile
  checkpointing/resume (`.compile/<stage>.json`); truncation detection;
  per-stage token/cost logging into `events.jsonl`; per-stage model
  routing (`STAGE_MODELS`); prompt/model provenance stamped into bundles.
- **Terminal surface (WS1b)**: readline history and multi-line paste
  submit in the agent; crash-proof turns that survive API errors with the
  transcript intact; session persistence in `$SYLABIS_HOME`.
- **Local web app security (WS1c)**: per-session token auth, Host-header
  validation (DNS-rebinding defense), CSRF/Origin checks on state-changing
  routes, request-size caps, generic error pages, background compiles
  with truthful stage progress.
- **MCP protocol hygiene (WS2)**: supported protocol versions echoed
  per-spec; unsupported-but-well-formed versions answered with the
  latest supported version and flagged loudly on stderr; malformed
  `protocolVersion` rejected as invalid params; `serve --mock` makes the
  MCP surface testable fully offline.
- **Textual TUI (WS1b)**: an optional full-screen terminal app
  (`pip install "sylabis[tui]"`) as a view over the same agent loop and
  tool registry — conversation pane, journey sidebar, and a submit
  editor that is input capture only (the guide never writes the
  learner's work). `SYLABIS_NO_TUI=1` keeps the plain ANSI agent.
- **Sandboxed rubric-script grading (WS3a)**: `sylabis/sandbox.py` runs
  bundle-declared scripts under bwrap/nsjail when available (no network,
  read-only rootfs, CPU/memory/pids limits) and *always* with an
  allowlisted environment — never the API key; script paths are
  containment-checked before anything executes; a first grade of an
  attached bundle that declares scripts requires one recorded consent
  per bundle; `--unsandboxed` (or no working sandbox) falls back loudly,
  naming the bundle's origin.
- **Copy-on-attach with provenance (WS3a)**: `sy attach` copies local
  paths (never symlinks — grading must not mutate the original
  checkout) and records source, pinned commit SHA, attach time, and
  fingerprints of pre-existing grades; knowledge evidence is
  origin-tagged, and grades that shipped *with* an attached bundle are
  excluded from `prior_knowledge()` until the learner earns them.
- **`sy publish` — the privacy scrubber (WS3b)**: an allowlist transform
  into a clean template (learner block, topic prompt, events, grades,
  artifacts, reflections, portfolio claims, and dotfiles structurally
  excluded), a mandatory machine-readable license (default CC-BY-4.0),
  and an all-or-nothing gate: self-test + secret scan + PII audit, with
  the template removed on any refusal. `okf.yaml` now inventories every
  doc with a SHA-256 hash (tamper-evident; `okf.hash_problems` verifies).
- **Structured outputs (WS4.1)**: one-shot stages send per-stage JSON
  Schemas (`prompts.STAGE_OUTPUT_SCHEMAS`) through the API's
  `output_config.format` constraint; the stdlib validation + one-round
  repair stays as the fallback and the mock-mode contract. Fixtures
  unchanged.
- **Agentic harvest + parallel emission (WS4.2)**: harvest is now
  propose → verify → consolidate — source verification runs concurrently
  (bounded pool of 8, cached in `.compile/verify_cache.json`) and
  unverifiable resolvable locators are dropped *before* sequencing
  (event-logged, with a keep-and-warn floor if more than half would
  drop); per-milestone lesson calls run under a bounded pool of 3, so
  compile latency stops being linear in course size (measured 2.97× on
  the lesson phase — see `docs/adr/001-agent-architecture.md`).
- **Grader calibration (WS4.3)**: an executable checkpoint with no
  rubric scripts is now recorded `unscored` (pass/fail from Tiers 1–2 +
  explain-back) instead of a fabricated 0.85; the unseeded three-tier
  fallback base is the raw claim-audit pass ratio, so a
  ~38%-claims-passing artifact can no longer clear the 0.75 threshold.
  Grader stages stamp model/prompt/version provenance into `grade.yaml`.
- **Docs**: `docs/adr/001-agent-architecture.md` (the no-framework
  decision, structured-outputs migration, and measured latency results),
  `docs/threat-model.md` (attach/grading/web protections mapped to OWASP
  Agentic ASI05/ASI04, residual risks stated), and
  `docs/publishing-guide.md` (the scrubber, the mandatory license, the
  locators-only legal posture, and the registry listing flow).

### Changed
- The MCP server reports the real package version in `serverInfo`
  (previously hardcoded `0.1.0`).
- Library code raises typed `SylabisError`s instead of `SystemExit`;
  in particular, an intake viability refusal now raises
  `CompileDeclined` (with the verdict and notes attached) so the web and
  MCP surfaces can render it.
- `llm.py` routes stages to models via `STAGE_MODELS` (Sonnet for
  generation, Haiku for claim-audit/explain-back); the single `MODEL`
  constant survives only as a deprecated alias.
- Compile events record `learner_profile_hash`, never the raw learner
  profile.

### Security
- Web surface CSRF/DNS-rebinding fixes (WS1c) — upgrade before exposing
  `sy web` on a shared machine.
- Rubric scripts no longer inherit the parent environment (including
  `ANTHROPIC_API_KEY`) and can no longer be declared at
  bundle-escaping paths — see the sandbox entry above and
  `docs/threat-model.md`.

## [0.1.0]

Internal prototype: compile → teach → grade → adapt → connect loop,
three surfaces (terminal agent, Reading Room web app, MCP server) over
one journey-scoped tool registry, fully offline fixture-driven test
suite.
