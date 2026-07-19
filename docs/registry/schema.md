# The registry index: `paths.json`

The sylabis registry is one static file — `paths.json` in the
`sylabis-registry` repo, served raw over GitHub Pages (crates.io
sparse-index style). Clients (`sy paths search` / `sy paths get`) cache
it under `$SYLABIS_HOME/.registry-cache.json` with a daily TTL and ETag
revalidation. The machine-readable schema lives at
[`registry/schema.json`](../../registry/schema.json); this page is the
human explanation.

Fetching the index sends **nothing but the GET** — no identifiers, no
telemetry. Cloning from the registry is free and anonymous forever;
publishing is what unlocks status (journey page, verified badge,
attribution, ranking). There is no hard gate.

## Top level

```json
{
  "schema": 1,
  "paths": [ { ...entry... } ]
}
```

`schema` is the index version; a client that doesn't understand it
refuses the index with a clear "upgrade sylabis" error rather than
misreading entries.

## Entry fields

```json
{
  "id": "dhruva/survey-synthesis",
  "name": "Survey Synthesis for Practitioners",
  "git_url": "https://github.com/dhruva/sylabis-path-survey-synthesis",
  "commit_sha": "0f9c2f7f1f4b1e6f8f0f9c2f7f1f4b1e6f8f0f9c",
  "topic": "survey methods, synthesis, research briefs",
  "est_hours": 12,
  "license": "CC-BY-4.0",
  "assumed_knowledge": ["spreadsheets"],
  "author": "dhruva",
  "verified": {
    "verified_at": "2026-07-19T00:00:00Z",
    "sylabis_version": "0.2.0",
    "checks_passed": ["pinned_sha", "self_test", "okf_conformance",
                      "okf_hashes", "pii_scan", "secret_scan",
                      "source_locators"]
  },
  "derived_from": "someone/an-earlier-path"
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Author-scoped listing id, `<author-slug>/<path-slug>` (lowercase, hyphens). The author segment must match the GitHub handle opening the listing PR — first PR from the matching handle wins the namespace. |
| `name` | yes | Human-readable course title. |
| `git_url` | yes | Public repo hosting the **published template** (output of `sy publish` — never a raw journey). Naming convention: `sylabis-path-<slug>`, Homebrew-tap style. |
| `commit_sha` | yes | The **pinned** full 40-char commit. `sy paths get` clones and checks out exactly this commit; if the repo's history no longer contains it (post-listing rewrite), the attach is refused with `sha mismatch`. Pinning makes tampering *evident*, not impossible — CI re-verification is the second layer. |
| `topic` | yes | Free-text topic line; `sy paths search` matches name, topic, tags, and id. |
| `est_hours` | yes | Total estimated hours across the path's milestones. |
| `license` | yes | SPDX id from the bundle's mandatory publish manifest (default `CC-BY-4.0`; `CC0-1.0` for a literal public-domain dedication). Bundles ship source **locators, never harvested source text** — linking is not copying — so the license covers the author's course structure and lesson text. |
| `assumed_knowledge` | no | Declared assumed-knowledge tags (these replaced the scrubbed learner block at publish time). |
| `author` | yes | Author handle; credited on listings and in attribution chains. |
| `verified` | CI-only | Badge fields written by registry CI on a green `verify-listing` run and refreshed by the re-verification cron. **Never author-set** — a PR that includes them is rejected. |
| `derived_from` | no | Attribution chain: the listing id or git URL this path builds on. Surfaced on listings — attribution is part of the status economy. |

## Producing an entry

`sy publish <course> --to <dir> --author <you> --list` prints a
ready-to-paste entry with everything except `commit_sha`, which only
exists after you push the template (`git rev-parse HEAD`) — pinning is
deliberately the author's step. See
[`registry/README.md`](../../registry/README.md) for the full listing
flow and the CI gate.
