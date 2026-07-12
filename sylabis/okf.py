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
"""
import time
from pathlib import Path

import yaml

OKF_VERSION = 1
# Frontmatter descriptions are summaries, not content. If truncation loses
# meaning, fix the caller's description, not this limit.
DESCRIPTION_LIMIT = 120

DOC_TYPES = ("course", "milestone", "source", "knowledge-bundle",
             "competency-evidence", "portfolio", "journey")


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


# ----------------------------------------------------------------- manifest

def emit_bundle_manifest(course_dir: Path) -> Path:
    """okf.yaml — machine-readable inventory of every OKF document in the
    bundle. Regenerated by scanning disk so it can never drift. Emit LAST."""
    course_dir = Path(course_dir)
    documents = []
    for md in sorted(course_dir.rglob("*.md")):
        meta, _ = parse_doc(md)
        if meta and meta.get("okf") == OKF_VERSION and meta.get("type") in DOC_TYPES:
            documents.append({
                "path": str(md.relative_to(course_dir)),
                "type": meta["type"],
                "title": meta.get("title", ""),
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

    okf_yaml = course_dir / "okf.yaml"
    if not okf_yaml.exists():
        problems.append("OKF: okf.yaml bundle manifest missing")
    else:
        inventory = yaml.safe_load(okf_yaml.read_text()) or {}
        for d in inventory.get("documents", []):
            if not (course_dir / d["path"]).exists():
                problems.append(f"OKF: okf.yaml lists {d['path']} "
                                f"but it is not on disk")
    return problems
