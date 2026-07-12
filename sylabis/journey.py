"""
Journey — the connected-curriculum layer. A journey is one learner's home
directory of courses plus the knowledge verified across them. Concepts
proven in one course become assumed knowledge in the next compile, so
curricula connect instead of restarting from zero. All state is read from
the course bundles on disk — the journey owns no database and no copies,
so it can never disagree with the courses it describes.
"""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from . import okf

COURSES_SUBDIR = "courses"


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


def attach(home_dir: Path, source: str) -> Path:
    """Connect content that lives elsewhere: a git URL clones, a local path
    symlinks. Either way the course joins the journey under courses/ and
    its verified knowledge counts like knowledge from any other course —
    the connection layer does not care which repo a course came from."""
    root = Path(home_dir) / COURSES_SUBDIR
    root.mkdir(parents=True, exist_ok=True)

    if source.startswith(_GIT_PREFIXES) or source.endswith(".git"):
        name = source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        dest = _unclaimed(root / (slugify(name) or "course"))
        proc = subprocess.run(["git", "clone", "--quiet", source, str(dest)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"git clone failed: {proc.stderr.strip()}")
        if not (dest / "course.yaml").exists():
            shutil.rmtree(dest)
            raise SystemExit(f"{source} is not a course bundle "
                             "(no course.yaml at its root)")
        return dest

    src = Path(source).expanduser().resolve()
    if not (src / "course.yaml").exists():
        raise SystemExit(f"{source} is not a course bundle "
                         "(no course.yaml at its root)")
    dest = _unclaimed(root / src.name)
    dest.symlink_to(src, target_is_directory=True)
    return dest


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
    grade — the same standard the portfolio uses, read from the same files."""
    entries: dict[str, dict] = {}
    for cdir in course_dirs(home_dir):
        manifest = yaml.safe_load((cdir / "course.yaml").read_text())
        title = manifest["meta"]["title"]
        for m in manifest.get("milestones", []):
            if not milestone_passed(cdir, m["id"]):
                continue
            g = yaml.safe_load((cdir / m["id"] / "grade.yaml").read_text()) or {}
            cp_path = cdir / m["id"] / "checkpoint.yaml"
            if not cp_path.exists():
                continue
            cp = yaml.safe_load(cp_path.read_text()) or {}
            for concept in cp.get("core_concepts") or []:
                key = " ".join(concept.lower().split())
                entry = entries.setdefault(key, {"concept": concept,
                                                 "evidence": []})
                entry["evidence"].append({
                    "course": cdir.name, "course_title": title,
                    "milestone_id": m["id"], "grade": g.get("grade"),
                    "graded_at": g.get("graded_at")})
    return sorted(entries.values(), key=lambda e: e["concept"].lower())


def prior_knowledge(home_dir: Path) -> list[str]:
    """What every new compile may assume: the verified concepts. This is
    the connection — course N+1 builds on what course N proved."""
    return [e["concept"] for e in knowledge(home_dir)]


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
