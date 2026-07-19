# sylabis-registry

> **Scaffolding note:** this directory ships inside the main `sylabis`
> repo as a template for the separate **`sylabis-registry`** repository.
> Nothing here runs in the sylabis repo itself — the workflow, PR
> template, and scripts only take effect once copied into the registry
> repo's root.

The community index of published sylabis paths. It is deliberately
**all static**: one `paths.json`, served raw via GitHub Pages
(crates.io sparse-index style, ETag-cacheable). No API server, no
accounts, no database.

## How cloning works (free, anonymous, forever)

```bash
sy paths search "quantization"     # fetches paths.json (cached daily)
sy paths get author/some-path      # clones the repo, checks out the
                                   # PINNED commit, joins your journey
```

Fetching the index sends nothing but the GET. Attaching clones a public
git repo. There is no gate, no login, and no telemetry — and
`sy attach <git-url>` always keeps working even without the registry.

## How listing works (this is the give-to-get)

Reciprocity here is **status, not access**. Publishing a path unlocks:

- your **public journey page** (`sy journey --publish` → GitHub Pages),
- the **verified badge** on your listing (set by CI, below),
- **attribution**: paths derived from yours carry `derived_from`,
  surfaced on their listings,
- **ranking weight** (listing order = verify-freshness + adoption).

To list a path:

1. `sy publish <course> --to <dir> --author <you> --list`
   — the scrubber produces a clean template (no learner data, ever) and
   prints a ready-to-paste `paths.json` entry.
2. Push the template to a public repo named **`sylabis-path-<slug>`**
   (Homebrew-tap convention). Pin it: `git rev-parse HEAD`.
3. Open a PR against this repo adding **one entry** to `paths.json`
   (the Obsidian community-plugins flow). Fill in the PR template
   checklist.

### Namespace convention

Listing ids are **author-scoped**: `<author-slug>/<path-slug>`. The
author segment must match the GitHub handle opening the listing PR;
first PR from the matching handle wins the namespace (anti-squatting).

## CI is the quality gate

Every listing PR runs `verify-listing` (`.github/workflows/`), which
for each changed entry:

1. validates the entry against `schema.json`,
2. clones `git_url` and checks out the **pinned** `commit_sha`
   (a repo that no longer contains it fails),
3. pip-installs sylabis and runs, all of which must be green:
   - `compiler.self_test` (structural + OKF conformance),
   - `okf.hash_problems` (tamper-evident per-file hashes),
   - `publish.pii_problems` (no learner data classes survive),
   - `publish.scan_secrets` (no keys/tokens/high-entropy secrets),
   - `verify.verify_sources` on the bundle's source locators,
4. on green, writes the `verified` badge fields
   (`verified_at`, `sylabis_version`, `checks_passed`) into the entry.

Structural review needs no human; license/name sanity is a 30-second
human merge. A re-verification cron re-runs the same checks to keep
badges fresh.

## Legal posture

Bundles ship **locators, never harvested source text** — linking is not
copying. The mandatory `license` field (default `CC-BY-4.0`;
`CC0-1.0` if you want a literal public-domain dedication) covers the
course structure and lesson text the author wrote, not the primary
sources the course points at.

## Files

| File | What it is |
|---|---|
| `paths.json` | the index (schema-versioned; `schema: 1`) |
| `schema.json` | machine-readable JSON Schema for `paths.json` |
| `.github/workflows/verify-listing.yml` | the CI quality gate |
| `.github/PULL_REQUEST_TEMPLATE.md` | listing PR checklist |
| `scripts/verify_listing.py` | the checks the workflow runs |
