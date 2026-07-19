# Publishing a learning path — how `sy publish` works

A used course bundle is radioactive: `course.yaml` embeds your learner
block (weekly hours, hardware, your full prior-knowledge list, the topic
prompt in your own words), `events.jsonl` is your activity log, and
`artifact.md` / `reflection.md` / `grade.yaml` / `portfolio/` are your
verbatim work. Publishing is therefore a **transform, never a copy**:

```
sy publish <course> --to <dir> [--author HANDLE] [--license SPDX-ID]
```

`sy publish` re-emits an explicit **allowlist** of course content into a
fresh template directory. Learner state is *structurally excluded* — the
builder never reads those files, so there is nothing to "filter out."
This is the tooling behind the rule "never commit a learner's journey."

## What ships (the whole list)

| Path | Notes |
|---|---|
| `course.yaml` | Constructed fresh: the learner block is **removed**, replaced by declared `assumed_knowledge` tags and the `target_artifact`; `topic_prompt` dropped; per-stage `meta.provenance` (model, prompt version, sylabis version) preserved; a **publish manifest** added |
| `<milestone>/LESSON.md`, `checkpoint.yaml`, `starter/` | The course content itself |
| `knowledge/*.md` | Source **locator** docs — see the legal posture below |
| `grader/sources.yaml` | Machine-readable locator fallback (no learner data) |
| `sidequests/*/sidequest.yaml` | Re-locked — unlock state is your progress, not course content |
| `.github/workflows/grade.yml` | The one allowed dotpath |
| `index.md`, `portfolio/`, `okf.yaml` | Regenerated pristine via `okf.py` (the sole frontmatter writer), with per-file SHA-256 hashes in `okf.yaml` |

## What never ships

`events.jsonl`, `grade.yaml`, `artifact.md`, `reflection.md`,
`portfolio/claims/`, `.compile/`, and **any dotfile** other than the
grade workflow. These are excluded by construction, and a
defense-in-depth gate re-checks the finished template anyway:

1. `compiler.self_test()` — the template must be a valid, startable
   bundle;
2. a **secret scan** (Anthropic/AWS/GitHub/Slack key shapes, private-key
   blocks, assignment-context and high-entropy tokens);
3. a **PII audit** that refuses if any excluded-class file, stray
   dotfile, learner block, or topic prompt survived.

Any failure raises `PublishError` and **removes the template** — a
refused publish leaves nothing behind.

## The license is mandatory

Every published path carries a machine-readable `license` field in its
publish manifest; publish refuses without one. The default is
**CC-BY-4.0** — free to share and adapt with attribution, which is what
keeps authorship visible as paths get forked and improved (attribution
is the fuel of the registry's status economy). Authors who want a
different grant — including a literal public-domain dedication — set it
explicitly (e.g. `--license CC0-1.0`). Path content is licensed
separately from the sylabis code (AGPL-3.0-only); see the README's
License section.

## Legal posture: locators, never source text

Published bundles ship **source locators** (arXiv IDs, DOIs, URLs) with
verification records — never harvested source text. Linking is not
copying. The publish manifest records `content: locators-only` plus a
summary of what `verify.py` concluded about every locator at harvest
time, so a consumer can see the source hygiene before attaching.

## Listing on the registry (high level)

Publishing produces a template directory; **listing** makes it
discoverable. The registry is fully serverless — a static `paths.json`
index over author-hosted bundle repos:

1. Push your published template to its own public git repo
   (convention: `sylabis-path-<slug>`).
2. Open a PR against the registry repo adding one `paths.json` entry
   (id, name, git URL, **pinned commit SHA**, topic, estimated hours,
   license, assumed-knowledge tags, author).
3. Registry CI clones your repo at the pinned SHA and runs the full
   quality gate — `self_test()`, OKF conformance, source-locator
   verification, and the same privacy/secret scan `sy publish` runs.
   Green CI sets the **verified badge**; a human merge is a 30-second
   license/name sanity check.
4. Learners find it with `sy paths search` and attach it with
   `sy paths get <id>`, which resolves at the pinned SHA.

Cloning from the registry is free and anonymous, forever. Publishing is
what unlocks the status layer — your public journey page
(`sy journey --publish`), the verified badge, and the attribution chain
on derived paths. For the listing schema, PR checklist, and
namespace/squatting policy, see [`docs/registry/`](registry/).
