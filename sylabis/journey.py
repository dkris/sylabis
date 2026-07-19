"""
Journey — the connected-curriculum layer. A journey is one learner's home
directory of courses plus the knowledge verified across them. Concepts
proven in one course become assumed knowledge in the next compile, so
curricula connect instead of restarting from zero. All state is read from
the course bundles on disk — the journey owns no database and no copies,
so it can never disagree with the courses it describes.
"""
import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from . import okf
from .errors import AttachError, PublishError

COURSES_SUBDIR = "courses"

# Provenance record written into every attached bundle (WS3a): where it
# came from, the commit it was pinned at, when it joined the journey,
# and a fingerprint of every grade record that PRE-DATED the attach —
# the mechanism prior_knowledge() uses to ignore grades the learner
# did not earn (see knowledge()).
ATTACH_PROVENANCE = ".sylabis-attach.yaml"


def home(explicit: Path | str | None = None) -> Path:
    """The learner's home: --home flag > $SYLABIS_HOME > ~/sylabis."""
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("SYLABIS_HOME", str(Path.home() / "sylabis")))


def slugify(topic: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return slug[:48] or "course"


def course_dirs(home_dir: Path) -> list[Path]:
    root = Path(home_dir) / COURSES_SUBDIR
    if not root.exists():
        return []
    return sorted(p.parent for p in root.glob("*/course.yaml"))


def course_dir(home_dir: Path, name: str) -> Path:
    return Path(home_dir) / COURSES_SUBDIR / name


def new_course_dir(home_dir: Path, topic: str) -> Path:
    """A fresh directory for a compile: the topic slug, suffixed on collision."""
    return _unclaimed(course_dir(home_dir, slugify(topic)))


def _unclaimed(base: Path) -> Path:
    out, n = base, 2
    while out.exists() or out.is_symlink():
        out = base.with_name(f"{base.name}-{n}")
        n += 1
    return out


_GIT_PREFIXES = ("http://", "https://", "git@", "ssh://", "file://")


def attach(home_dir: Path, source: str, pin_sha: str | None = None) -> Path:
    """Connect content that lives elsewhere: a git URL clones, a local
    path COPIES. Never a symlink: grading and path actuation mutate the
    attached bundle, and must never corrupt the original checkout
    (WS3a copy-on-attach). Either way the course joins the journey under
    courses/ with a provenance record (source, pinned commit, attach
    time, preexisting-grade fingerprints) and its verified knowledge
    counts like knowledge from any other course — once the learner earns
    it (see knowledge()).

    pin_sha (WS3c, registry `get`): a listing pins CONTENT, not a
    branch. After the clone, the pinned commit is checked out; when the
    repo's history no longer contains it (post-listing rewrite, or a
    bogus sha in the index) the attach is refused with
    AttachError('sha mismatch...') and nothing joins the journey."""
    root = Path(home_dir) / COURSES_SUBDIR
    root.mkdir(parents=True, exist_ok=True)

    if source.startswith(_GIT_PREFIXES) or source.endswith(".git"):
        name = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        dest = _unclaimed(root / (slugify(name) or "course"))
        proc = subprocess.run(["git", "clone", "--quiet", source, str(dest)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise AttachError(f"git clone failed: {proc.stderr.strip()}")
        if pin_sha:
            _checkout_pinned(dest, source, pin_sha)
        if not (dest / "course.yaml").exists():
            shutil.rmtree(dest)
            raise AttachError(f"{source} is not a course bundle "
                              "(no course.yaml at its root)")
        sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"],
                             capture_output=True, text=True)
        commit = sha.stdout.strip() if sha.returncode == 0 else None
        _record_attach(dest, source, commit)
        return dest

    if pin_sha:
        raise AttachError(f"pin_sha requires a git source; {source} is a "
                          "local path (copied, not cloned)")
    src = Path(source).expanduser().resolve()
    if not (src / "course.yaml").exists():
        raise AttachError(f"{source} is not a course bundle "
                          "(no course.yaml at its root)")
    dest = _unclaimed(root / src.name)
    shutil.copytree(src, dest, symlinks=True)
    _record_attach(dest, source, commit=None)
    return dest


def _checkout_pinned(dest: Path, source: str, pin_sha: str) -> None:
    """Check the pinned commit out inside the fresh clone. A clone only
    carries reachable history, so a post-listing history rewrite (or a
    sha the repo never had) fails the containment check here — the
    tamper-evidence mechanism behind registry pinning. On failure the
    clone is removed: a refused attach leaves nothing behind."""
    have = subprocess.run(
        ["git", "-C", str(dest), "cat-file", "-e", f"{pin_sha}^{{commit}}"],
        capture_output=True, text=True)
    if have.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise AttachError(
            f"sha mismatch: {source} no longer contains pinned commit "
            f"{pin_sha} — its history changed since the listing was "
            "verified; refusing to attach unpinned content")
    co = subprocess.run(["git", "-C", str(dest), "checkout", "--quiet",
                         "--detach", pin_sha],
                        capture_output=True, text=True)
    if co.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise AttachError(f"sha mismatch: could not check out pinned commit "
                          f"{pin_sha}: {co.stderr.strip()}")


def _record_attach(dest: Path, source: str, commit: str | None) -> None:
    """Provenance stamp inside the attached copy. preexisting_grades
    fingerprints (SHA-256) every grade.yaml present AT attach time: a
    grade the learner later earns rewrites that file (new graded_at,
    attempt), so a changed hash is the signal that the work was done
    here, while an unchanged hash marks a grade that arrived with the
    bundle — possibly hand-edited by its author — which must never seed
    the learner's prior knowledge."""
    pre = {p.parent.name: hashlib.sha256(p.read_bytes()).hexdigest()
           for p in sorted(dest.glob("*/grade.yaml"))}
    (dest / ATTACH_PROVENANCE).write_text(yaml.dump({
        "schema": 1,
        "source": source,
        "commit": commit,
        "attached_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "preexisting_grades": pre,
    }, default_flow_style=False))


def attach_info(cdir: Path) -> dict | None:
    """The attach provenance record for a course, or None when the
    course is local (compiled in this journey)."""
    p = Path(cdir) / ATTACH_PROVENANCE
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text()) or {}


def milestone_passed(cdir: Path, milestone_id: str) -> bool:
    gpath = Path(cdir) / milestone_id / "grade.yaml"
    if not gpath.exists():
        return False
    return bool((yaml.safe_load(gpath.read_text()) or {}).get("passed"))


def course_next(cdir: Path) -> dict:
    """The next milestone for one course. Status is one of
    ready | blocked | complete; ready carries the milestone fields."""
    cdir = Path(cdir)
    manifest = yaml.safe_load((cdir / "course.yaml").read_text())
    for m in manifest["milestones"]:
        if milestone_passed(cdir, m["id"]):
            continue
        deps = m.get("depends_on", [])
        if all(milestone_passed(cdir, d) for d in deps):
            return {"status": "ready", "course": cdir.name,
                    "course_title": manifest["meta"]["title"],
                    "milestone_id": m["id"], "title": m["title"],
                    "estimated_hours": m.get("estimated_hours")}
        return {"status": "blocked", "course": cdir.name,
                "course_title": manifest["meta"]["title"],
                "milestone_id": m["id"], "blocked_on": deps}
    return {"status": "complete", "course": cdir.name,
            "course_title": manifest["meta"]["title"]}


def next_steps(home_dir: Path) -> list[dict]:
    """One entry per course in the journey — course_next() for each."""
    return [course_next(c) for c in course_dirs(home_dir)]


def submittable(home_dir: Path) -> list[dict]:
    """Ready milestones whose required files are already on disk — what
    `sylabis submit` can grade without being told a path or an id."""
    out = []
    for step in next_steps(home_dir):
        if step["status"] != "ready":
            continue
        cdir = course_dir(home_dir, step["course"])
        cp_path = cdir / step["milestone_id"] / "checkpoint.yaml"
        if not cp_path.exists():
            continue
        cp = yaml.safe_load(cp_path.read_text()) or {}
        required = (cp.get("structural") or {}).get("required_files", [])
        missing = [f for f in required
                   if not (cdir / step["milestone_id"] / f).exists()]
        if missing:
            out.append({**step, "status": "awaiting_work", "missing": missing})
        else:
            out.append({**step, "missing": []})
    return out


def knowledge(home_dir: Path) -> list[dict]:
    """Verified concepts across every course, each with its evidence trail.
    A concept is verified when a milestone that introduces it has a passing
    grade — the same standard the portfolio uses, read from the same files.

    Every evidence row carries `origin`: 'local' for courses compiled in
    this journey, 'attached:<source>' for attached bundles. Evidence in an
    attached bundle additionally carries `preexisting: True` when its
    grade.yaml is byte-identical to the fingerprint recorded at attach
    time — a grade that shipped WITH the bundle, not one the learner
    earned here. Doing the attached course's work rewrites grade.yaml,
    so earned grades never match the fingerprint and count fully."""
    entries: dict[str, dict] = {}
    for cdir in course_dirs(home_dir):
        manifest = yaml.safe_load((cdir / "course.yaml").read_text())
        title = manifest["meta"]["title"]
        info = attach_info(cdir)
        origin = f"attached:{info.get('source', '?')}" if info else "local"
        pre = (info or {}).get("preexisting_grades") or {}
        for m in manifest.get("milestones", []):
            if not milestone_passed(cdir, m["id"]):
                continue
            gpath = cdir / m["id"] / "grade.yaml"
            g = yaml.safe_load(gpath.read_text()) or {}
            cp_path = cdir / m["id"] / "checkpoint.yaml"
            if not cp_path.exists():
                continue
            cp = yaml.safe_load(cp_path.read_text()) or {}
            row = {"course": cdir.name, "course_title": title,
                   "milestone_id": m["id"], "grade": g.get("grade"),
                   "graded_at": g.get("graded_at"), "origin": origin}
            if m["id"] in pre and hashlib.sha256(
                    gpath.read_bytes()).hexdigest() == pre[m["id"]]:
                row["preexisting"] = True
            for concept in cp.get("core_concepts") or []:
                key = " ".join(concept.lower().split())
                entry = entries.setdefault(key, {"concept": concept,
                                                 "evidence": []})
                entry["evidence"].append(dict(row))
    return sorted(entries.values(), key=lambda e: e["concept"].lower())


def prior_knowledge(home_dir: Path) -> list[str]:
    """What every new compile may assume: the verified concepts. This is
    the connection — course N+1 builds on what course N proved.

    Excludes concepts whose ONLY evidence is a pre-existing grade record
    in an attached bundle (grade.yaml unchanged since attach — see
    knowledge()): a hand-edited `passed: true` in someone else's bundle
    must never seed the next compile. Concepts the learner verifies by
    doing the attached course's work count fully."""
    return [e["concept"] for e in knowledge(home_dir)
            if any(not ev.get("preexisting") for ev in e["evidence"])]


def emit_map(home_dir: Path) -> Path | None:
    """knowledge.md at the journey root — the learner-readable map of what
    is proven and where courses connect. Regenerable from disk state alone;
    call after anything that changes a grade or adds a course."""
    home_dir = Path(home_dir)
    dirs = course_dirs(home_dir)
    if not dirs:
        return None
    know = knowledge(home_dir)

    lines = ["# Knowledge map", "", "## Courses", ""]
    for step in next_steps(home_dir):
        if step["status"] == "complete":
            note = "complete"
        elif step["status"] == "blocked":
            note = f"blocked on {', '.join(step['blocked_on'])}"
        else:
            note = f"next: {step['milestone_id']} — {step['title']}"
        lines.append(f"- [{step['course_title']}]"
                     f"({COURSES_SUBDIR}/{step['course']}/index.md) — {note}")

    lines += ["", "## Verified concepts", ""]
    if not know:
        lines.append("Nothing verified yet — pass a milestone to start the map.")
    for e in know:
        refs = ", ".join(
            f"[{ev['course_title']} · {ev['milestone_id']}]"
            f"({COURSES_SUBDIR}/{ev['course']}/portfolio/claims/{ev['milestone_id']}.md)"
            for ev in e["evidence"])
        lines.append(f"- **{e['concept']}** — {refs}")

    bridges = [e for e in know
               if len({ev["course"] for ev in e["evidence"]}) > 1]
    if bridges:
        lines += ["", "## Connections", ""]
        for e in bridges:
            names = sorted({ev["course_title"] for ev in e["evidence"]})
            if len(names) < 2:  # identical titles: fall back to dir names
                names = sorted({ev["course"] for ev in e["evidence"]})
            lines.append(f"- **{e['concept']}** links {' and '.join(names)}")

    return okf.write_doc(
        home_dir / "knowledge.md", "journey", "Knowledge map",
        f"{len(know)} verified concepts across "
        f"{len(dirs)} course{'s' if len(dirs) != 1 else ''}.",
        "\n".join(lines),
        course_count=len(dirs),
        concept_count=len(know),
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


# ------------------------------------------------- public journey page (WS3c)

# The two files a published journey page consists of — nothing else is
# ever written into the destination.
PAGE_FILES = ("knowledge.md", "index.html")

# A learner-work line shorter than this is too generic to grep for
# (bullets, "yes", headings); longer lines are treated as verbatim
# learner text that must never reach the public page.
_WORK_FRAGMENT_MIN = 12


def publish_page(home_dir: Path, dest: Path | str) -> Path:
    """`sy journey --publish DIR` — emit a SCRUBBED public journey page:
    a knowledge.md (OKF frontmatter via okf.py, the sole frontmatter
    writer) plus a minimal static index.html rendering it, suitable for
    a public GitHub Pages repo. Publishing this page is what listing a
    path unlocks — reciprocity as status, never as access.

    Concept names, course titles, and grades are the POINT of the page
    and stay. What must never appear: artifact/reflection text, learner
    block fields (hardware, raw topic prompt), or journey-home paths.
    The page is built in a staging dir and gated (page_problems +
    publish.scan_secrets) BEFORE anything touches dest — a refused
    publish writes nothing at all."""
    import tempfile

    from .publish import scan_secrets  # lazy: journey stays light

    home_dir = Path(home_dir)
    dest = Path(dest)
    dirs = course_dirs(home_dir)
    if not dirs:
        raise PublishError("nothing to publish — the journey has no courses")
    know = knowledge(home_dir)
    body = _page_body(next_steps(home_dir), know)

    with tempfile.TemporaryDirectory() as td:
        staging = Path(td) / "page"
        staging.mkdir()
        okf.write_doc(
            staging / "knowledge.md", "journey", "Knowledge map",
            f"{len(know)} verified concepts across "
            f"{len(dirs)} course{'s' if len(dirs) != 1 else ''}.",
            body,
            course_count=len(dirs),
            concept_count=len(know),
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        (staging / "index.html").write_text(_page_html(body))

        problems = page_problems(home_dir, staging) + scan_secrets(staging)
        if problems:
            raise PublishError(
                "journey page refused — learner data would reach the "
                "public page:\n" + "\n".join(f"  - {p}" for p in problems))
        dest.mkdir(parents=True, exist_ok=True)
        for name in PAGE_FILES:
            shutil.copyfile(staging / name, dest / name)
    return dest


def _page_body(steps: list[dict], know: list[dict]) -> str:
    """The public map body. Unlike emit_map (which links into the local
    bundle tree), every reference here is plain text — a public page
    must carry no journey filesystem links at all."""
    lines = ["# Knowledge map", "",
             "Every grade below references a verifiable competency claim "
             "in the course bundle that produced it.", "", "## Courses", ""]
    for step in steps:
        if step["status"] == "complete":
            note = "complete"
        elif step["status"] == "blocked":
            note = f"blocked on {', '.join(step['blocked_on'])}"
        else:
            note = f"in progress — next: {step['milestone_id']}"
        lines.append(f"- **{step['course_title']}** — {note}")

    lines += ["", "## Verified concepts", ""]
    if not know:
        lines.append("Nothing verified yet — pass a milestone to "
                     "start the map.")
    for e in know:
        refs = "; ".join(
            f"{ev['course_title']} · {ev['milestone_id']} "
            f"({_grade_label(ev.get('grade'))})"
            for ev in e["evidence"])
        lines.append(f"- **{e['concept']}** — {refs}")

    bridges = [e for e in know
               if len({ev["course"] for ev in e["evidence"]}) > 1]
    if bridges:
        lines += ["", "## Connections", ""]
        for e in bridges:
            names = sorted({ev["course_title"] for ev in e["evidence"]})
            if len(names) < 2:
                names = sorted({ev["course"] for ev in e["evidence"]})
            lines.append(f"- **{e['concept']}** links {' and '.join(names)}")
    return "\n".join(lines)


def _grade_label(grade) -> str:
    if isinstance(grade, (int, float)):
        return f"grade {grade:.0%}"
    return "passed, unscored"


def _page_html(body_md: str) -> str:
    # Lazy import: web pulls the whole surface stack, and importing it
    # at module load would create an import cycle (web imports journey).
    from .web import md_to_html
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Knowledge map</title>
<style>
  body {{ margin: 2rem auto; max-width: 42rem; padding: 0 1rem;
         font: 16px/1.6 system-ui, sans-serif; color: #1a1a1a; }}
  h1, h2 {{ line-height: 1.25; }}
  a {{ color: #0a5c8c; }}
  footer {{ margin-top: 3rem; font-size: 0.85rem; color: #666; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #111; color: #ddd; }}
    a {{ color: #7cc0e8; }}
    footer {{ color: #999; }}
  }}
</style>
</head>
<body>
<main>
{md_to_html(body_md)}
</main>
<footer>Generated by <code>sy journey --publish</code> — grades reference
verifiable competency claims in the underlying course bundles.</footer>
</body>
</html>
"""


def page_problems(home_dir: Path, page_dir: Path) -> list[str]:
    """The journey-page leak gate: no artifact/reflection sentence, no
    learner-block field value (hardware, raw topic prompt), and no
    journey-home path may appear in any emitted page file. Concept
    names, course titles, and grades are the page's purpose, not
    learner data — they are deliberately NOT flagged. Problem strings
    name the leak class, never the leaked text itself."""
    home_dir = Path(home_dir)
    forbidden: list[tuple[str, str]] = []
    for cdir in course_dirs(home_dir):
        for work in sorted(cdir.glob("*/artifact.md")) + \
                sorted(cdir.glob("*/reflection.md")):
            rel = f"{cdir.name}/{work.parent.name}/{work.name}"
            for line in work.read_text(errors="replace").splitlines():
                line = line.strip()
                if len(line) >= _WORK_FRAGMENT_MIN:
                    forbidden.append((f"learner work ({rel})", line))
        manifest = yaml.safe_load((cdir / "course.yaml").read_text()) or {}
        hw = str((manifest.get("learner") or {}).get("hardware") or "").strip()
        if len(hw) >= 4:
            forbidden.append((f"learner hardware ({cdir.name})", hw))
        prompt = str((manifest.get("meta") or {})
                     .get("topic_prompt") or "").strip()
        if len(prompt) >= 4:
            forbidden.append((f"learner topic prompt ({cdir.name})", prompt))
    if home_dir.is_absolute():
        forbidden.append(("journey home path", str(home_dir)))
    forbidden.append(("journey home path", str(home_dir.resolve())))

    problems: list[str] = []
    for f in sorted(Path(page_dir).rglob("*")):
        if not f.is_file():
            continue
        text = f.read_text(errors="replace")
        for label, needle in forbidden:
            hit = f"page: {f.name} leaks {label}"
            if needle in text and hit not in problems:
                problems.append(hit)
    return problems
