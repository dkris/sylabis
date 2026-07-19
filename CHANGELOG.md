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

### Changed
- The MCP server reports the real package version in `serverInfo`
  (previously hardcoded `0.1.0`).

### Security
- Web surface CSRF/DNS-rebinding fixes (WS1c) — upgrade before exposing
  `sy web` on a shared machine.

## [0.1.0]

Internal prototype: compile → teach → grade → adapt → connect loop,
three surfaces (terminal agent, Reading Room web app, MCP server) over
one journey-scoped tool registry, fully offline fixture-driven test
suite.
