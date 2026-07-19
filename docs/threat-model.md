# sylabis threat model — attach, grading, and the local web surface

This note maps the Phase-1 protections to the [OWASP Agentic Top
10](https://owasp.org/) categories they answer, and states the residual
risks honestly. It covers the point where strangers' content enters the
system: `sy attach` + `sy submit` (bundle-declared rubric scripts), the
publish/registry sharing path, and the local web app. The publish-side
privacy controls are documented in the
[publishing guide](publishing-guide.md).

## The threat that shaped Phase 1

A course bundle is a directory a stranger can author. Grading an
`executable`-mode checkpoint runs scripts **the bundle author wrote** on
the learner's machine. Before the sandbox work, `sy attach
<hostile-url>` followed by `sy submit` was a working
remote-code-execution and API-key-theft chain: scripts ran with the full
parent environment (including `ANTHROPIC_API_KEY`) and unvalidated
paths. Everything below exists because the first hostile bundle arrives
with the first stranger.

## ASI05 — Unauthorized code execution

Bundle-declared rubric scripts are treated as untrusted code. Defenses,
layered so the weakest never silently stands in for the strongest
(`sylabis/sandbox.py`, `sylabis/grader.py`):

| Control | Mechanism |
|---|---|
| **Environment scrub — always** | Scripts get an allowlisted env (`PATH`, `HOME`, `TMPDIR` — `sandbox.ENV_ALLOWLIST`) regardless of backend. `ANTHROPIC_API_KEY` and the rest of `os.environ` are withheld unconditionally. |
| **OS sandbox when available** | `SandboxRunner` wraps execution in **bwrap** or **nsjail** when one is on PATH *and passes a probe* (a binary present but broken must fall back loudly, not fail every grade mysteriously): no network (`--unshare-all`), read-only rootfs, tmpfs `/tmp`, cleared env. |
| **Resource ceilings** | rlimits on CPU (300 s, matching `SCRIPT_TIMEOUT`), address space (4 GiB), and process count (256), applied in the child; nsjail gets the equivalent flags. |
| **Path containment** | Script paths are bundle-declared and therefore attacker-controlled. Absolute paths, `../` escapes past the course bundle, and nonexistent targets are refused with a typed error **before anything executes** — the same resolve + `is_relative_to` containment the tool routes use. |
| **Consent gate** | The first grade of an *attached* bundle that declares rubric scripts raises `ConsentRequired`: the scripts and the bundle's origin are listed, the learner approves once, and the decision is recorded per bundle (`.sylabis-trust`). Nothing executes without recorded trust. |
| **Timeout** | `SCRIPT_TIMEOUT = 300 s` per script; a timeout is a flagged grading failure, never a hang. |
| **Loud fallback** | No working sandbox (or the documented `--unsandboxed` flag) runs the script directly with the scrubbed env and prints a warning **naming the bundle's origin**, so the learner knows exactly whose code just ran uncontained. |

Adversarial tests in the offline suite cover the escape attempts:
`../`-escaping and absolute script paths are refused, and an executed
script cannot read `ANTHROPIC_API_KEY`.

## ASI04 — Supply chain (attached bundles and the registry)

An attached bundle is third-party supply chain: its content feeds
lessons, its grade records could feed the learner's knowledge graph, and
its scripts could feed the grader. Defenses (`sylabis/journey.py`,
`sylabis/okf.py`):

| Control | Mechanism |
|---|---|
| **Copy-on-attach** | `journey.attach()` **copies** local paths and clones git URLs — never a symlink. Grading and path actuation mutate the attached copy and can never corrupt someone else's shared checkout. |
| **Pinned SHAs + provenance** | Every attach writes `.sylabis-attach.yaml` into the copy: source URL/path, the commit SHA at clone time, and the attach timestamp. Registry `get` resolves at the listed pinned SHA, so a post-listing tampered repo is caught by SHA mismatch. |
| **Origin tagging** | Every evidence row in `knowledge()` carries `origin: local` or `origin: attached:<source>`, so provenance is visible wherever knowledge is rendered. |
| **Pre-existing-grade exclusion** | The attach record fingerprints (SHA-256) every `grade.yaml` present *at attach time*. A grade whose file is still byte-identical to that fingerprint shipped **with** the bundle — possibly hand-edited by its author — and is excluded from `prior_knowledge()`, so it can never seed the learner's next compile. Doing the attached course's work rewrites `grade.yaml`, breaks the fingerprint match, and counts fully. |
| **okf.yaml integrity hashes** | `okf.yaml` inventories every OKF document with a SHA-256 hash (`okf.emit_bundle_manifest`); `okf.hash_problems()` verifies them at publish/attach boundaries. Tamper-**evident**, not tamper-proof — see residual risks. |
| **Registry CI gate** | A listing PR only earns the verified badge after CI clones the bundle at the pinned SHA and runs `self_test()`, OKF conformance, source-locator verification, and the publish-time privacy/secret scan. See `docs/registry/`. |

## The local web surface — CSRF and DNS rebinding

The pre-fix `sy web` had no auth: any web page the user visited could
POST to `127.0.0.1:8787/learn|/submit|/chat`, spending API credits and
mutating the journey, and DNS rebinding reached it remotely — the same
bug class as CVE-2025-49596 against Anthropic's own MCP Inspector. The
fix is the **Jupyter pattern**, wholesale (`sylabis/web.py`):

- a random per-session **bearer token**, printed once as a tokenized URL
  at startup and exchanged for an `HttpOnly` session cookie — every
  route requires it;
- **Host-header validation** (only `localhost`/`127.0.0.1[:port]`) — the
  DNS-rebinding defense; binding to 127.0.0.1 alone does not provide it;
- **Origin checks** on POSTs and a **CSRF token** on all HTML forms;
- request-body size caps, generic 500 pages (no path leaks), CSP and
  `X-Content-Type-Options` headers, no third-party font fetch.

Checks run in order: Host first (rebinding), then token-or-cookie, then
Origin (cross-site forgery), then CSRF inside form routes. The test
suite includes real-transport forged cross-origin POSTs and wrong-Host
requests asserting 403.

## Residual risks — stated honestly

- **`--unsandboxed` exists.** It is a documented, deliberate escape
  hatch (some machines have neither bwrap nor nsjail, and user
  namespaces can be disabled). It always scrubs the environment, but the
  script then has full filesystem and network access. The warning names
  the bundle's origin; the residual risk is the learner ignoring it.
- **Hashes are tamper-evidence, not proof.** Whoever can edit a
  document in a bundle can also regenerate `okf.yaml`. Integrity hashes
  catch accidental drift and lazy tampering; they do not prove
  authorship. **Cryptographic signing arrives with Phase-2 identity** —
  claiming more than tamper-evidence today would be dishonest.
- **The sandbox is a mitigation, not a boundary guarantee.** bwrap/nsjail
  containment is strong but kernel-attack-surface-dependent; the hosted
  product (Phase 2) moves Tier-3 execution to gVisor and then
  one-shot microVMs for exactly this reason.
- **Pre-existing-grade exclusion protects the knowledge graph, not the
  display.** An attached bundle's shipped grades still render in that
  course's own views (flagged by origin); they just never propagate.
