# List a path

One entry per PR, appended to `paths.json`. CI (`verify-listing`) does
the structural work; a human only sanity-checks license and naming.

## Checklist

- [ ] The bundle repo is public and named `sylabis-path-<slug>`
- [ ] The bundle was produced by `sy publish` (never a raw journey copy
      — no `events.jsonl`, `grade.yaml`, `artifact.md`, `reflection.md`,
      or learner block anywhere in it)
- [ ] `commit_sha` is the full 40-char sha of the commit I am listing
      (`git rev-parse HEAD`), and I will not rewrite history under it
- [ ] `id` is `<my-github-handle-slug>/<path-slug>` — the author
      segment matches the account opening this PR
- [ ] `license` matches the `publish.license` field inside the bundle's
      `course.yaml` (bundles ship source locators, never source text)
- [ ] `assumed_knowledge` tags honestly describe what the path assumes
- [ ] If this path builds on someone else's, `derived_from` credits it
- [ ] I did **not** fill in the `verified` block (CI writes it)

## What happens next

- `verify-listing` clones your repo at the pinned sha and runs
  self-test, OKF conformance + hash checks, the PII and secret scans,
  and source-locator verification.
- Green CI sets the `verified` badge fields on your entry; a maintainer
  then merges after a quick license/name sanity check.
- A red run annotates what failed — fix the bundle, push, re-pin the
  new sha, and update the entry.
