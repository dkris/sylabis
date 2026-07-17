"""
OKF bundle producer. Owns ALL markdown-with-frontmatter production for a
course bundle — no other module writes frontmatter; it all goes through here.
The bundle on disk IS the credential: every human-readable document carries
OKF frontmatter, and okf.yaml inventories them so a claim can be verified
without any external authority.

Document types:
  course               index.md
  milestone            <milestone>/LESSON.md
  source               knowledge/source-<id>.md
  knowledge-bundle     knowledge/index.md
  competency-evidence  portfolio/claims/<milestone>.md
  portfolio            portfolio/index.md
  journey              <journey home>/knowledge.md (cross-course, journey.py)
  grade-report         portfolio/reports/<milestone>.md — the shareable,
                       self-contained record of one grading (pass or fail)
  readme               README.md at the bundle root — what GitHub renders
                       when a published course is shared by its repo URL
"""
import hashlib
import json
import time
from pathlib import Path

import yaml

from . import config

OKF_VERSION = 1
# Frontmatter descriptions are summaries, not content. If truncation loses
# meaning, fix the caller's description, not this limit.
DESCRIPTION_LIMIT = 120

DOC_TYPES = ("course", "milestone", "source", "knowledge-bundle",
             "competency-evidence", "portfolio", "journey",
             "grade-report", "readme")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def frontmatter(doc_type: str, title: str, description: str, **fields) -> str:
    if doc_type not in DOC_TYPES:
        raise ValueError(f"unknown OKF doc type: {doc_type}")
    meta = {
        "okf": OKF_VERSION,
        "type": doc_type,
        "title": title,
        "description": " ".join((description or "").split())[:DESCRIPTION_LIMIT],
    }
    meta.update(fields)
    return ("---\n"
            + yaml.dump(meta, default_flow_style=False, sort_keys=False,
                        allow_unicode=True)
            + "---\n")


def write_doc(path: Path, doc_type: str, title: str, description: str,
              body: str, **fields) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter(doc_type, title, description, **fields)
                    + "\n" + body.strip() + "\n")
    return path


def parse_doc(path: Path) -> tuple[dict | None, str]:
    """Split an OKF document into (frontmatter, body).
    Returns (None, full_text) when the file carries no parseable frontmatter."""
    text = Path(path).read_text()
    if not text.startswith("---\n"):
        return None, text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return None, text
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None, text
    if not isinstance(meta, dict):
        return None, text
    return meta, parts[2]


def strip_frontmatter(text: str) -> str:
    """Drop a leading frontmatter block from model output. The lesson model
    is asked to prepend the canonical block verbatim but occasionally
    reformats it; the canonical block from frontmatter() always wins."""
    t = text.lstrip()
    if t.startswith("---"):
        end = t.find("\n---", 3)
        if end != -1:
            return t[end + 4:].lstrip("\n")
    return text


# ---------------------------------------------------------------- documents

def emit_milestone_doc(course_dir: Path, milestone: dict, course_title: str,
                       lesson_body: str) -> Path:
    return write_doc(
        Path(course_dir) / milestone["id"] / "LESSON.md",
        "milestone", milestone["title"], milestone["artifact_spec"],
        strip_frontmatter(lesson_body),
        id=milestone["id"],
        course=course_title,
        estimated_hours=milestone["estimated_hours"],
        artifact_type=milestone["artifact_type"],
    )


def milestone_frontmatter(milestone: dict, course_title: str) -> str:
    """The block handed to the lesson prompt for verbatim prepending."""
    return frontmatter(
        "milestone", milestone["title"], milestone["artifact_spec"],
        id=milestone["id"], course=course_title,
        estimated_hours=milestone["estimated_hours"],
        artifact_type=milestone["artifact_type"])


def emit_knowledge_bundle(course_dir: Path, harvest: dict,
                          course_title: str) -> None:
    course_dir = Path(course_dir)
    lines = []
    for s in harvest["sources"]:
        desc = s.get("okf_description") or s.get("what_learner_needs", "")
        verified = s.get("verified")
        write_doc(
            course_dir / "knowledge" / f"source-{s['id']}.md",
            "source", s["title"], desc,
            "\n".join([
                f"**{s['title']}** — {s.get('author', '')} ({s.get('year', '')})",
                "",
                f"- Locator: `{s['locator']}`",
                f"- Authority: {s.get('authority', '')}",
                f"- Freshness: {s.get('freshness_class', '')}",
                f"- Verified: {_verified_label(s)}",
                "",
                f"**What you need from it:** {s.get('what_learner_needs', '')}",
            ]),
            id=s["id"],
            source_type=s.get("type", ""),
            locator=s["locator"],
            authority=s.get("authority"),
            freshness_class=s.get("freshness_class"),
            verified=verified,
            verification=s.get("verification", ""),
        )
        mark = {True: "", False: " ⚠ unverified", None: " (unchecked)"}[verified]
        lines.append(f"- [{s['title']}](source-{s['id']}.md) — "
                     f"`{s['locator']}`{mark}")
    write_doc(course_dir / "knowledge" / "index.md",
              "knowledge-bundle", f"Knowledge base — {course_title}",
              f"Primary sources this course compiles from ({len(lines)}).",
              "# Knowledge base\n\n" + "\n".join(lines),
              source_count=len(harvest["sources"]))


def _verified_label(source: dict) -> str:
    v = source.get("verified")
    if v is True:
        return "yes"
    if v is None:
        return "not checked"
    return f"NO — {source.get('verification', 'unverified')}"


def emit_course_index(course_dir: Path) -> Path:
    """Regenerable from disk state alone: course.yaml + grade.yaml files +
    sidequest lock state. Call whenever progress or unlocks change."""
    course_dir = Path(course_dir)
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
    meta = manifest["meta"]

    total_hours = sum(m.get("estimated_hours") or 0
                      for m in manifest["milestones"])
    lines = [f"# {meta['title']}", "",
             manifest.get("learner", {}).get("target_artifact", ""), "",
             f"Compiled {meta['compiled_at']} from prompt: "
             f"“{meta.get('topic_prompt', '')}”", "",
             "## Milestones", ""]
    passed_count = 0
    for m in manifest["milestones"]:
        status = _milestone_status(course_dir, m["id"])
        if status.startswith("passed"):
            passed_count += 1
        lines.append(f"- [{m['id']} — {m['title']}]({m['id']}/LESSON.md) "
                     f"(~{m['estimated_hours']}h) — {status}")

    sidequests = _sidequest_states(course_dir)
    if sidequests:
        lines += ["", "## Sidequests", ""]
        for sq in sidequests:
            if sq.get("locked", True):
                lines.append(f"- {sq['title']} — locked")
            else:
                lines.append(f"- **[{sq['title']}]"
                             f"(sidequests/{sq['id']}/sidequest.yaml)** — "
                             f"{sq.get('hook', '')} (unlocked)")

    lines += ["", "## Bundle", "",
              "- [Knowledge base](knowledge/index.md)",
              "- [Portfolio](portfolio/index.md)",
              "- Event log: `events.jsonl`"]

    return write_doc(
        course_dir / "index.md", "course", meta["title"],
        manifest.get("learner", {}).get("target_artifact", meta["title"]),
        "\n".join(lines),
        domain=meta.get("domain"),
        version=meta.get("version"),
        compiled_at=meta.get("compiled_at"),
        milestone_count=len(manifest["milestones"]),
        milestones_passed=passed_count,
        estimated_hours=total_hours,
    )


def _milestone_status(course_dir: Path, milestone_id: str) -> str:
    gpath = course_dir / milestone_id / "grade.yaml"
    if not gpath.exists():
        return "not started"
    g = yaml.safe_load(gpath.read_text()) or {}
    if g.get("passed"):
        return f"passed ({g.get('grade', 0):.0%})"
    return f"attempt {g.get('attempt', 1)} — not yet"


def _sidequest_states(course_dir: Path) -> list[dict]:
    sq_root = course_dir / "sidequests"
    if not sq_root.exists():
        return []
    states = []
    for sq_yaml in sorted(sq_root.glob("*/sidequest.yaml")):
        states.append(yaml.safe_load(sq_yaml.read_text()) or {})
    return states


def emit_claim_doc(course_dir: Path, checkpoint: dict, result: dict) -> Path:
    """Competency evidence for a passed milestone. The claim is verifiable
    from the bundle itself: artifact, reflection, grade, and event log."""
    course_dir = Path(course_dir)
    mid = result["milestone_id"]
    claims = result.get("verified_claims", [])
    body = [f"# Verified: {mid}", ""]
    body += [f"- {c}" for c in claims]
    body += ["", "## Evidence", "",
             f"- Artifact: [`{mid}/artifact.md`](../../{mid}/artifact.md)",
             f"- Reflection: [`{mid}/reflection.md`](../../{mid}/reflection.md)",
             f"- Grade record: [`{mid}/grade.yaml`](../../{mid}/grade.yaml)",
             f"- Graded {result['graded_at']}, attempt {result['attempt']}, "
             f"grade {result['grade']:.0%}"]
    return write_doc(
        course_dir / "portfolio" / "claims" / f"{mid}.md",
        "competency-evidence",
        f"Competency evidence — {mid}",
        claims[0] if claims else f"Passed {mid}",
        "\n".join(body),
        milestone_id=mid,
        artifact_type=checkpoint.get("artifact_type", ""),
        grade=result["grade"],
        attempt=result["attempt"],
        graded_at=result["graded_at"],
    )


def emit_portfolio_index(course_dir: Path) -> Path:
    course_dir = Path(course_dir)
    claims_dir = course_dir / "portfolio" / "claims"
    entries = []
    if claims_dir.exists():
        for doc in sorted(claims_dir.glob("*.md")):
            meta, _ = parse_doc(doc)
            if meta and meta.get("type") == "competency-evidence":
                entries.append(meta)
    body = ["# Portfolio", ""]
    if entries:
        body += [f"- [{e['title']}](claims/{e['milestone_id']}.md) — "
                 f"grade {e['grade']:.0%}, {e['graded_at']}"
                 for e in entries]
    else:
        body.append("No verified claims yet. Pass a milestone to earn one.")
    return write_doc(
        course_dir / "portfolio" / "index.md", "portfolio",
        "Portfolio", f"{len(entries)} verified claims.",
        "\n".join(body),
        claim_count=len(entries),
    )


def pct(value) -> str:
    """One percent formatter for report, README, and index — the numbers a
    learner shares must never drift between documents."""
    return f"{float(value or 0):.0%}"


def badge_markdown(grade: float, passed: bool) -> str:
    """A static shields.io badge line — pure string formatting; sylabis
    never fetches it, GitHub's renderer does."""
    label = pct(grade).replace("%", "%25")
    if passed:
        color = "brightgreen" if grade >= 0.90 else "green"
        text = "passed"
    else:
        color, text = "red", "not yet"
    return (f"![grade: {pct(grade)} — {text}]"
            f"(https://img.shields.io/badge/grade-{label}_{text.replace(' ', '_')}-{color})")


def _sha256(path: Path) -> str | None:
    path = Path(path)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _grade_history(course_dir: Path, milestone_id: str) -> list[dict]:
    """Every attempt of one milestone, reconstructed from the append-only
    event log — the only place attempt history survives grade.yaml's
    overwrite. Degrades to [] on a damaged log; a report nicety must never
    crash grading."""
    log = Path(course_dir) / "events.jsonl"
    if not log.exists():
        return []
    rows = []
    try:
        for line in log.read_text().splitlines():
            e = json.loads(line)
            if e.get("type") == "milestone.graded" and \
                    e.get("payload", {}).get("milestone_id") == milestone_id:
                rows.append({"ts": e.get("ts", ""), **e["payload"]})
    except (json.JSONDecodeError, TypeError, KeyError):
        return []
    return rows


def emit_grade_report(course_dir: Path, checkpoint: dict, result: dict) -> Path:
    """The shareable grade record: portfolio/reports/<milestone>.md, one per
    milestone, overwritten per attempt exactly like grade.yaml (history
    lives in the table below, from events.jsonl). Every value is copied
    verbatim from the grade result — this emitter formats, never computes
    or re-derives a score."""
    course_dir = Path(course_dir)
    mid = result["milestone_id"]
    body = [f"# Grade report — {mid}", "",
            badge_markdown(result["grade"], result["passed"]), "",
            f"**{pct(result['grade'])} — "
            f"{'PASSED' if result['passed'] else 'NOT YET'}** · "
            f"attempt {result['attempt']} · graded {result['graded_at']}", ""]

    body += ["## Tiers", "",
             "| Check | Result |", "|---|---|",
             f"| Tier 1 — structural | "
             f"{'passed' if result.get('tier_1_passed') else 'FAILED'} |"]
    if "claim_audit" in result:
        s = result["claim_audit"]
        body.append(f"| Tier 2 — claim audit | "
                    f"{'passed' if result.get('tier_2_passed') else 'FAILED'}"
                    f" ({s['passed']}/{s['total']} claims) |")
    if result.get("tier_3_mode"):
        body.append(f"| Tier 3 — rubric ({result['tier_3_mode']}) | "
                    f"{pct(result['grade'])} |")
    for d in (result.get("tier_3") or {}).get("dimensions", []):
        body.append(f"| &nbsp;&nbsp;· {d['name']} | {d['score']:.2f} "
                    f"(nearest: {d.get('nearest_exemplar', '?')}) |")
    for note in result.get("rubric_scripts", []):
        body.append(f"| &nbsp;&nbsp;· script | {note} |")

    if result.get("explain_back"):
        body += ["", "## Explain-back", ""]
        for p in result["explain_back"]:
            body.append(f"- **{p['concept']}** — {p['verdict']}")
            if p["verdict"] != "understood" and p.get("followup_question"):
                body.append(f"  - Next question to sit with: "
                            f"{p['followup_question']}")
    if result.get("failure_flags"):
        body += ["", "## Flags", ""]
        body += [f"- `{f}`" for f in result["failure_flags"]]
    if result.get("verified_claims"):
        body += ["", "## Verified claims", ""]
        body += [f"- {c}" for c in result["verified_claims"]]

    history = _grade_history(course_dir, mid)
    if history:
        body += ["", "## Attempt history", "",
                 "| Attempt | Grade | Result | When |", "|---|---|---|---|"]
        body += [f"| {h.get('attempt')} | {pct(h.get('grade'))} | "
                 f"{'passed' if h.get('passed') else 'not yet'} | "
                 f"{h.get('ts')} |" for h in history]

    art_sha = _sha256(course_dir / mid / "artifact.md")
    refl_sha = _sha256(course_dir / mid / "reflection.md")
    body += ["", "## Evidence", "",
             f"- Artifact: [`{mid}/artifact.md`](../../{mid}/artifact.md)"
             + (f" — sha256 `{art_sha}`" if art_sha else ""),
             f"- Reflection: [`{mid}/reflection.md`](../../{mid}/reflection.md)"
             + (f" — sha256 `{refl_sha}`" if refl_sha else ""),
             f"- Grade record: [`{mid}/grade.yaml`](../../{mid}/grade.yaml)",
             f"- Event log: [`events.jsonl`](../../events.jsonl)", "",
             "## How to verify", "",
             "This report is tamper-evident, not signed: recompute the "
             "sha256 of the artifact and reflection and compare them to the "
             "hashes above, read the append-only `events.jsonl` for the "
             "full grading history, and for CI-graded attempts inspect the "
             "repository's git log and the Actions run that produced the "
             "grade commit."]

    return write_doc(
        course_dir / "portfolio" / "reports" / f"{mid}.md",
        "grade-report", f"Grade report — {mid}",
        f"{pct(result['grade'])} "
        f"{'passed' if result['passed'] else 'not yet'}, "
        f"attempt {result['attempt']}",
        "\n".join(body),
        milestone_id=mid,
        attempt=result["attempt"],
        grade=result["grade"],
        passed=result["passed"],
        graded_at=result["graded_at"],
        tier_1_passed=result.get("tier_1_passed", False),
        tier_2_passed=result.get("tier_2_passed", False),
        tier_3_mode=result.get("tier_3_mode"),
        artifact_type=checkpoint.get("artifact_type", ""),
        artifact_sha256=art_sha,
        reflection_sha256=refl_sha,
    )


def emit_repo_readme(course_dir: Path) -> Path:
    """README.md at the bundle root — the landing page GitHub renders when
    a published course is shared by its bare repo URL. Outward-facing where
    index.md is learner-facing. Regenerable from disk state alone; a
    half-graded bundle must render, not crash."""
    course_dir = Path(course_dir)
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
    meta = manifest["meta"]
    milestones = manifest.get("milestones", [])

    passed, concepts = 0, set()
    rows = []
    for m in milestones:
        gpath = course_dir / m["id"] / "grade.yaml"
        g = (yaml.safe_load(gpath.read_text()) or {}) if gpath.exists() else {}
        if g.get("passed"):
            passed += 1
            cp_path = course_dir / m["id"] / "checkpoint.yaml"
            if cp_path.exists():
                cp = yaml.safe_load(cp_path.read_text()) or {}
                concepts.update(cp.get("core_concepts") or [])
            rows.append(f"- [x] [{m['id']} — {m['title']}]({m['id']}/LESSON.md)"
                        f" — passed {pct(g.get('grade'))} "
                        f"([report](portfolio/reports/{m['id']}.md))")
        elif g:
            rows.append(f"- [ ] [{m['id']} — {m['title']}]({m['id']}/LESSON.md)"
                        f" — attempt {g.get('attempt', 1)}, not yet "
                        f"([report](portfolio/reports/{m['id']}.md))")
        else:
            # Honest completeness: hiding unfinished work is embellishment.
            rows.append(f"- [ ] [{m['id']} — {m['title']}]({m['id']}/LESSON.md)"
                        f" — not yet attempted")

    total = len(milestones)
    badges = (f"![milestones](https://img.shields.io/badge/"
              f"milestones-{passed}%2F{total}-"
              f"{'brightgreen' if total and passed == total else 'blue'}) "
              f"![verified concepts](https://img.shields.io/badge/"
              f"verified_concepts-{len(concepts)}-blue)")

    body = [f"# {meta['title']}", "", badges, "",
            manifest.get("learner", {}).get("target_artifact", ""), "",
            f"A [sylabis]({config.SYLABIS_REPO}) course bundle — "
            f"compiled {meta.get('compiled_at', '')}, graded milestone by "
            f"milestone from the real artifacts in this repository.", "",
            "## Milestones", ""]
    body += rows
    body += ["", "## In this repository", "",
             "- [Course index](index.md) — the learner-facing overview",
             "- [Portfolio](portfolio/index.md) — verified competency claims",
             "- [Grade reports](portfolio/reports/) — one shareable report "
             "per milestone",
             "- [Knowledge base](knowledge/index.md) — the primary sources",
             "- `events.jsonl` — the append-only grading history", "",
             "## Attestation", "",
             "Grades are produced by `sylabis grade` (three deterministic-"
             "first tiers plus an explain-back check). Pushes of work are "
             "graded by CI (`.github/workflows/grade.yml`); those grade "
             "commits are authored by `sylabis-grader` with the run "
             "recorded in the Actions tab. Each grade report carries the "
             "sha256 of the artifact it graded. This repository includes "
             "the learner's working notes and reflections, not just the "
             "results."]

    return write_doc(
        course_dir / "README.md", "readme", meta["title"],
        manifest.get("learner", {}).get("target_artifact", meta["title"]),
        "\n".join(body),
        milestones_passed=passed,
        milestone_count=total,
        concept_count=len(concepts),
    )


# ----------------------------------------------------------------- manifest

def emit_bundle_manifest(course_dir: Path) -> Path:
    """okf.yaml — machine-readable inventory of every OKF document in the
    bundle. Regenerated by scanning disk so it can never drift. Emit LAST."""
    course_dir = Path(course_dir)
    documents = []
    for md in sorted(course_dir.rglob("*.md")):
        if ".git" in md.relative_to(course_dir).parts:
            continue
        meta, _ = parse_doc(md)
        if meta and meta.get("okf") == OKF_VERSION and meta.get("type") in DOC_TYPES:
            documents.append({
                "path": str(md.relative_to(course_dir)),
                "type": meta["type"],
                "title": meta.get("title", ""),
                # the tamper-evidence anchor: edit a doc after emission and
                # conformance flags the mismatch
                "sha256": _sha256(md),
            })
    manifest = {
        "okf": OKF_VERSION,
        "generated_at": _now(),
        "documents": documents,
    }
    path = course_dir / "okf.yaml"
    path.write_text(yaml.dump(manifest, default_flow_style=False,
                              sort_keys=False, allow_unicode=True))
    return path


def conformance_problems(course_dir: Path) -> list[str]:
    """OKF conformance for self-test. Parses frontmatter with a real YAML
    parser rather than substring checks."""
    course_dir = Path(course_dir)
    problems = []

    required = {
        "index.md": "course",
        "knowledge/index.md": "knowledge-bundle",
        "portfolio/index.md": "portfolio",
    }
    for rel, expected in required.items():
        p = course_dir / rel
        if not p.exists():
            problems.append(f"OKF: {rel} missing")
            continue
        meta, _ = parse_doc(p)
        if meta is None:
            problems.append(f"OKF: {rel} has no parseable frontmatter")
        elif meta.get("type") != expected:
            problems.append(f"OKF: {rel} type is {meta.get('type')!r}, "
                            f"expected {expected!r}")

    manifest_path = course_dir / "course.yaml"
    if manifest_path.exists():
        manifest = yaml.safe_load(manifest_path.read_text())
        for m in manifest.get("milestones", []):
            lesson = course_dir / m["id"] / "LESSON.md"
            if not lesson.exists():
                continue  # structural check reports the missing file
            meta, _ = parse_doc(lesson)
            if meta is None:
                problems.append(f"OKF: {m['id']}/LESSON.md has no parseable "
                                f"frontmatter")
                continue
            if meta.get("type") != "milestone":
                problems.append(f"OKF: {m['id']}/LESSON.md type is "
                                f"{meta.get('type')!r}")
            if meta.get("id") != m["id"]:
                problems.append(f"OKF: {m['id']}/LESSON.md frontmatter id "
                                f"mismatch: {meta.get('id')!r}")
            if len(meta.get("description") or "") > DESCRIPTION_LIMIT:
                problems.append(f"OKF: {m['id']}/LESSON.md description "
                                f"exceeds {DESCRIPTION_LIMIT} chars")

    sources_yaml = course_dir / "grader" / "sources.yaml"
    if sources_yaml.exists():
        harvest = yaml.safe_load(sources_yaml.read_text()) or {}
        for s in harvest.get("sources", []):
            doc = course_dir / "knowledge" / f"source-{s['id']}.md"
            if not doc.exists():
                problems.append(f"OKF: knowledge/source-{s['id']}.md missing")

    readme = course_dir / "README.md"
    if readme.exists():
        # Never required (old bundles predate it) but when present it must
        # be the outward-facing OKF doc, not a stray file.
        meta, _ = parse_doc(readme)
        if meta is None or meta.get("type") != "readme":
            problems.append("OKF: README.md is not a 'readme' OKF document")

    okf_yaml = course_dir / "okf.yaml"
    if not okf_yaml.exists():
        problems.append("OKF: okf.yaml bundle manifest missing")
    else:
        inventory = yaml.safe_load(okf_yaml.read_text()) or {}
        listed = set()
        for d in inventory.get("documents", []):
            listed.add(d["path"])
            doc = course_dir / d["path"]
            if not doc.exists():
                problems.append(f"OKF: okf.yaml lists {d['path']} "
                                f"but it is not on disk")
            elif d.get("sha256") and _sha256(doc) != d["sha256"]:
                # hash-less entries (legacy manifests) stay conformant
                problems.append(f"OKF: {d['path']} was modified after "
                                f"okf.yaml was emitted (hash mismatch)")
        # The other direction: an OKF doc on disk that the inventory
        # doesn't know about is a hole in the credential.
        for md in sorted(course_dir.rglob("*.md")):
            rel = md.relative_to(course_dir)
            if ".git" in rel.parts:
                continue
            meta, _ = parse_doc(md)
            if (meta and meta.get("okf") == OKF_VERSION
                    and meta.get("type") in DOC_TYPES
                    and str(rel) not in listed):
                problems.append(f"OKF: {rel} is on disk but missing "
                                f"from okf.yaml")
    return problems
