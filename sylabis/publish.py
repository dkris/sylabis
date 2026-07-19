"""
`sy publish` — the privacy scrubber (Primetime plan, Phase 1 WS3b).

A used bundle is radioactive: course.yaml embeds the learner block
(weekly_hours, hardware, prior_knowledge, topic_prompt), events.jsonl
is the learner's activity log, and artifact.md / reflection.md /
grade.yaml / portfolio/ are the learner's verbatim work. Publishing is
therefore a TRANSFORM, never a copy of the journey: an explicit
ALLOWLIST of course content is re-emitted into a fresh template
directory, and learner state is structurally excluded — it is never
read by the builder, not filtered out of a copy. Journey-home files
(.session.json, .history, knowledge.md) live outside course dirs and
are additionally caught by the no-dotfiles rule if one ever strays in.

Shipped (the whole list):
  course.yaml            constructed fresh: learner block replaced by
                         top-level assumed_knowledge tags (from the
                         learner's declared prior_knowledge) +
                         target_artifact; topic_prompt dropped;
                         meta.provenance preserved; a publish manifest
                         added (author, MANDATORY license, sylabis
                         version, source-verification summary)
  <milestone>/LESSON.md, checkpoint.yaml, starter/   course content
  knowledge/*.md         source locator docs — bundles ship LOCATORS,
                         never harvested source text
  grader/sources.yaml    machine-readable locator fallback (no learner data)
  sidequests/*/sidequest.yaml   re-locked (unlock state is progress)
  .github/workflows/grade.yml   the one allowed dotpath
  index.md, portfolio/, okf.yaml  regenerated pristine via okf.py
  (okf.py stays the only frontmatter writer)

Pre-publish gate, all-or-nothing: compiler.self_test() on the template,
then a secret scan (Anthropic/AWS/GitHub/Slack key shapes, private-key
blocks, assignment-context and high-entropy tokens), then a
sylabis-specific PII audit that refuses if any excluded-class file, any
stray dotfile, or a learner block survived. Any failure raises
PublishError and removes the template — a refused publish leaves
nothing behind.

Integrity: the regenerated okf.yaml inventories every doc with a
SHA-256 hash (see okf.emit_bundle_manifest). Tamper-EVIDENT, not
tamper-proof.
"""
import math
import re
import shutil
from pathlib import Path

import yaml

from . import __version__
from . import events
from . import okf
from .errors import PublishError

DEFAULT_LICENSE = "CC-BY-4.0"

# Files that carry per-milestone course content (everything else in a
# milestone directory — artifact.md, reflection.md, grade.yaml — is the
# learner's and is never read).
_MILESTONE_FILES = ("LESSON.md", "checkpoint.yaml")

# The one dotpath a template may contain.
_ALLOWED_DOTPATHS = {
    Path(".github"),
    Path(".github/workflows"),
    Path(".github/workflows/grade.yml"),
}

# Learner-state filenames that must never appear anywhere in a template.
_EXCLUDED_NAMES = {"events.jsonl", "grade.yaml", "artifact.md",
                   "reflection.md"}


# ------------------------------------------------------------------ publish

def publish_course(course_dir: Path | str, dest: Path | str,
                   author: str = "",
                   license: str = DEFAULT_LICENSE) -> Path:
    """Scrub a course bundle into a shareable template at `dest`.

    Raises PublishError (and removes `dest`) when the license is empty,
    the template fails self-test, the secret scan hits, or any learner
    data survives the transform. Returns the template path on success."""
    course_dir = Path(course_dir).resolve()
    dest = Path(dest).resolve()
    if not (course_dir / "course.yaml").exists():
        raise PublishError(f"{course_dir} is not a course bundle "
                           "(no course.yaml at its root)")
    if not (license or "").strip():
        raise PublishError("license is a mandatory publish field "
                           f"(default {DEFAULT_LICENSE}); refusing to "
                           "publish an unlicensed template")
    if dest == course_dir or dest.is_relative_to(course_dir):
        raise PublishError("destination must be outside the course bundle")
    if dest.exists() and not dest.is_dir():
        raise PublishError(f"destination {dest} exists and is not a directory")
    if dest.is_dir() and any(dest.iterdir()):
        raise PublishError(f"destination {dest} exists and is not empty")
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())

    dest.mkdir(parents=True, exist_ok=True)
    try:
        _build_template(course_dir, manifest, dest, author.strip(),
                        license.strip())
        _gate(dest)
    except BaseException:
        # A refused (or crashed) publish must leave no partial template —
        # a half-scrubbed tree is exactly the leak this module prevents.
        shutil.rmtree(dest, ignore_errors=True)
        raise
    events.emit(course_dir, "course.published",
                {"dest": str(dest), "license": license.strip(),
                 "author": author.strip(),
                 "sylabis_version": __version__})
    return dest


# ------------------------------------------------------------ the transform

def _build_template(src: Path, manifest: dict, dest: Path,
                    author: str, license: str) -> None:
    learner = manifest.get("learner") or {}
    meta = {k: v for k, v in (manifest.get("meta") or {}).items()
            if k != "topic_prompt"}  # the learner's own words — never ships
    harvest = _read_sources(src)
    template = {
        "meta": meta,  # keeps title/domain/version/compiled_at/provenance
        # Declared assumed-knowledge tags replace the learner block: what
        # the course assumes, not who compiled it.
        "assumed_knowledge": list(learner.get("prior_knowledge") or []),
        "target_artifact": learner.get("target_artifact", ""),
        "viability": manifest.get("viability"),
        "publish": {
            "author": author,
            "license": license,  # mandatory, validated by the caller
            "sylabis_version": __version__,
            "published_at": okf._now(),
            # Registry-facing: what verify.py concluded about the bundle's
            # source locators at harvest time.
            "source_verification": _verification_summary(
                harvest.get("sources", [])),
            # Legal posture: linking is not copying.
            "content": "locators-only",
        },
        "milestones": manifest.get("milestones", []),
    }
    (dest / "course.yaml").write_text(
        yaml.dump(template, default_flow_style=False, sort_keys=False,
                  allow_unicode=True))

    for m in manifest.get("milestones", []):
        msrc, mdst = src / m["id"], dest / m["id"]
        mdst.mkdir(parents=True, exist_ok=True)
        for name in _MILESTONE_FILES:
            if (msrc / name).exists():
                shutil.copyfile(msrc / name, mdst / name)
        if (msrc / "starter").is_dir():
            shutil.copytree(msrc / "starter", mdst / "starter",
                            ignore=_no_dotfiles, dirs_exist_ok=True)

    if (src / "knowledge").is_dir():
        (dest / "knowledge").mkdir(exist_ok=True)
        for doc in sorted((src / "knowledge").glob("*.md")):
            shutil.copyfile(doc, dest / "knowledge" / doc.name)

    if (src / "grader" / "sources.yaml").exists():
        (dest / "grader").mkdir(exist_ok=True)
        shutil.copyfile(src / "grader" / "sources.yaml",
                        dest / "grader" / "sources.yaml")

    for sq_yaml in sorted(src.glob("sidequests/*/sidequest.yaml")):
        sq = yaml.safe_load(sq_yaml.read_text()) or {}
        sq.pop("unlocked_at", None)  # unlock state is learner progress
        sq["locked"] = True
        out = dest / "sidequests" / sq_yaml.parent.name
        out.mkdir(parents=True, exist_ok=True)
        (out / "sidequest.yaml").write_text(
            yaml.dump(sq, default_flow_style=False))

    wf = src / ".github" / "workflows" / "grade.yml"
    if wf.exists():
        (dest / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wf, dest / ".github" / "workflows" / "grade.yml")

    # Pristine learner-state scaffolding + regenerated docs — all
    # frontmatter production routes through okf.py (the invariant).
    (dest / "portfolio").mkdir(exist_ok=True)
    (dest / "portfolio" / "state.yaml").write_text(
        yaml.dump({"entries": []}, default_flow_style=False))
    okf.emit_portfolio_index(dest)
    okf.emit_course_index(dest)
    okf.emit_bundle_manifest(dest)  # hashes every doc; emit LAST


def _no_dotfiles(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n.startswith(".")}


def _read_sources(src: Path) -> dict:
    p = src / "grader" / "sources.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def _verification_summary(sources: list[dict]) -> dict:
    counts = {"verified": 0, "unverified": 0, "flagged_search": 0,
              "unchecked": 0}
    for s in sources:
        v = s.get("verified")
        if v is True:
            counts["verified"] += 1
        elif v is None:
            counts["unchecked"] += 1
        elif s.get("locator_kind") == "search" or \
                s.get("verification") == "search_locator_model_unsure":
            counts["flagged_search"] += 1
        else:
            counts["unverified"] += 1
    counts["total"] = len(sources)
    return counts


# ----------------------------------------------------------------- the gate

def _gate(template: Path) -> None:
    from .compiler import self_test  # local import: compiler is heavier
    problems = self_test(template)
    if problems:
        raise PublishError("publish refused — template fails self-test:\n"
                           + "\n".join(f"  - {p}" for p in problems))
    leaks = scan_secrets(template)
    if leaks:
        raise PublishError("publish refused — secret scan hit:\n"
                           + "\n".join(f"  - {p}" for p in leaks))
    pii = pii_problems(template)
    if pii:
        raise PublishError("publish refused — learner data survived the "
                           "scrub:\n" + "\n".join(f"  - {p}" for p in pii))


# Secret shapes. Assignment-context and entropy rules deliberately skip
# pure-hex runs so okf.yaml's sha256 inventory never false-positives.
_SECRET_PATTERNS = [
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("aws_access_key_id",
     re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|A3T[A-Z0-9])[A-Z0-9]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("private_key_block",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("assigned_secret",
     re.compile(r"(?i)\b(?:api[_-]?key|apikey|secret|passwd|password|"
                r"auth[_-]?token|access[_-]?token|credential)\b"
                r"['\"]?\s*[:=]\s*['\"]?(?=\S*\d)[A-Za-z0-9+/_-]{16,}")),
]
_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_ENTROPY_THRESHOLD = 4.2  # bits/char; pure hex tops out at 4.0


def _shannon(s: str) -> float:
    n = len(s)
    return -sum((c / n) * math.log2(c / n)
                for c in (s.count(ch) for ch in set(s)))


def scan_secrets(root: Path) -> list[str]:
    """Regex + entropy secret scan over every file under root.
    Returns problem strings naming the file and the pattern class —
    never the matched value (the report must not relay the secret)."""
    root = Path(root)
    problems = []
    for p in sorted(rp for rp in root.rglob("*") if rp.is_file()):
        rel = p.relative_to(root)
        text = p.read_text(errors="replace")
        for name, pat in _SECRET_PATTERNS:
            if pat.search(text):
                problems.append(f"secret: {rel} matches {name}")
        for tok in _ENTROPY_CANDIDATE.findall(text):
            if re.fullmatch(r"[0-9a-fA-F]+", tok):
                continue  # hex digest (okf.yaml hashes, git SHAs)
            if (tok.lower() != tok and tok.upper() != tok
                    and any(c.isdigit() for c in tok)
                    and _shannon(tok) > _ENTROPY_THRESHOLD):
                problems.append(f"secret: {rel} contains a high-entropy "
                                f"token ({len(tok)} chars)")
                break  # one report per file is enough
    return problems


def pii_problems(root: Path) -> list[str]:
    """Sylabis-specific PII audit of a template tree: refuses any
    excluded-class learner file, any dotfile beyond the grade workflow,
    and a surviving learner block / topic prompt / missing license in
    course.yaml. Defense-in-depth — the builder never reads these files,
    so a hit here means the transform itself regressed."""
    root = Path(root)
    problems = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            if rel not in _ALLOWED_DOTPATHS:
                problems.append(f"pii: dotfile survived the scrub: {rel}")
            continue
        if p.name in _EXCLUDED_NAMES:
            problems.append(f"pii: excluded-class file survived: {rel}")
        if rel.parts[:2] == ("portfolio", "claims") and p.is_file():
            problems.append(f"pii: learner portfolio claim survived: {rel}")
        # .compile/ (raw stage outputs incl. learner constraints) starts
        # with a dot, so the dotfile rule above already refuses it.

    cy = root / "course.yaml"
    if not cy.exists():
        problems.append("pii: course.yaml missing from template")
        return problems
    manifest = yaml.safe_load(cy.read_text()) or {}
    if "learner" in manifest:
        problems.append("pii: learner block survived in course.yaml")
    if "topic_prompt" in (manifest.get("meta") or {}):
        problems.append("pii: topic_prompt survived in course.yaml meta")
    pub = manifest.get("publish") or {}
    if not (pub.get("license") or "").strip():
        problems.append("pii: publish manifest missing mandatory license")
    return problems
