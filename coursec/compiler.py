"""
Compiler pipeline: intake -> harvest -> sequence -> emit -> self-test.
Output is a course directory matching the course-template schema.
"""
import time
from pathlib import Path

import yaml

from . import events
from .llm import LLM, parse_json
from . import prompts


def compile_course(topic: str, profile: dict, out_dir: Path, llm: LLM) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    events.emit(out_dir, "compile.requested",
                {"topic": topic, "profile": profile})

    # Stage 1 — Intake (includes viability check)
    print("[1/5] Intake + viability...")
    spec = parse_json(llm.call("intake", prompts.INTAKE_SYSTEM,
                               f"Topic: {topic}\nLearner profile: {profile}"))
    if spec["viability"]["verdict"] == "decline":
        raise SystemExit(f"Declined: {spec['viability']['notes']}")
    print(f"      domain={spec['domain']} "
          f"checkable={spec['viability']['verifiable_skeleton_pct']}% "
          f"grader={spec['viability']['grader_mode']}")

    # Stage 2 — Harvest
    print("[2/5] Source harvest...")
    harvest = parse_json(llm.call("harvest", prompts.HARVEST_SYSTEM,
                                  f"Compilation spec: {spec}"))
    print(f"      {len(harvest['sources'])} sources")

    # Stage 3 — Sequence
    print("[3/5] Sequencing...")
    seq = parse_json(llm.call("sequence", prompts.SEQUENCE_SYSTEM,
                              f"Spec: {spec}\nSources: {harvest}"))
    _check_dag(seq["milestones"])
    print(f"      {len(seq['milestones'])} milestones, "
          f"{len(seq.get('sidequests', []))} sidequests")

    # Stage 4 — Emit
    print("[4/5] Emitting course repo...")
    _emit_repo(out_dir, topic, spec, harvest, seq, llm)

    # Stage 5 — Self-test (structural)
    print("[5/5] Self-test...")
    problems = self_test(out_dir)
    if problems:
        raise SystemExit(f"Self-test FAILED — course not shipped:\n" +
                         "\n".join(f"  - {p}" for p in problems))

    events.emit(out_dir, "compile.completed", {
        "milestone_count": len(seq["milestones"]),
        "viability_pct": spec["viability"]["verifiable_skeleton_pct"],
        "grader_mode": spec["viability"]["grader_mode"],
    })
    print(f"\nCourse compiled: {out_dir}")
    return out_dir


def _check_dag(milestones: list) -> None:
    ids = {m["id"] for m in milestones}
    seen = set()
    for m in milestones:  # milestones arrive in order; deps must precede
        for dep in m.get("depends_on", []):
            if dep not in ids:
                raise SystemExit(f"Sequencing error: {m['id']} depends on unknown {dep}")
            if dep not in seen:
                raise SystemExit(f"Sequencing error: {m['id']} depends on later milestone {dep}")
        seen.add(m["id"])


def _emit_repo(out: Path, topic: str, spec: dict, harvest: dict,
               seq: dict, llm: LLM) -> None:
    grader_mode = spec["viability"]["grader_mode"]

    # course.yaml
    manifest = {
        "meta": {"title": spec["topic"], "domain": spec["domain"],
                 "version": "0.1.0",
                 "compiled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "topic_prompt": topic},
        "learner": spec.get("constraints", {}) | {
            "prior_knowledge": spec.get("assumed_knowledge", []),
            "target_artifact": spec["target_artifact"]},
        "viability": spec["viability"],
        "milestones": [],
    }

    sq_by_parent = {}
    for sq in seq.get("sidequests", []):
        sq_by_parent.setdefault(sq["parent"], {})[sq["type"]] = sq["id"]
        sq_dir = out / "sidequests" / sq["id"]
        sq_dir.mkdir(parents=True, exist_ok=True)
        (sq_dir / "sidequest.yaml").write_text(yaml.dump(
            {**sq, "locked": True}, default_flow_style=False))

    for m in seq["milestones"]:
        m_dir = out / m["id"]
        (m_dir / "starter").mkdir(parents=True, exist_ok=True)

        m_sources = [s for s in harvest["sources"] if s["id"] in m["source_ids"]]
        lesson = llm.call(f"lesson_{m['id']}", prompts.LESSON_SYSTEM,
                          f"Spec: {spec}\nMilestone: {m}\nSources: {m_sources}")
        (m_dir / "LESSON.md").write_text(lesson)

        checkpoint = {
            "milestone_id": m["id"],
            "artifact_type": m["artifact_type"],
            "artifact_spec": m["artifact_spec"],
            "grader_type": grader_mode,
            "structural": {"required_files": ["artifact.md", "reflection.md"]
                           if grader_mode == "three_tier"
                           else ["reflection.md"]},
            "misconception_target": m["misconception_target"],
            "core_concepts": m["core_concepts"],
            "pass_threshold": 0.75,
        }
        (m_dir / "checkpoint.yaml").write_text(
            yaml.dump(checkpoint, default_flow_style=False))

        manifest["milestones"].append({
            "id": m["id"], "title": m["title"],
            "estimated_hours": m["estimated_hours"],
            "artifact_type": m["artifact_type"],
            "checkpoint": f"{m['id']}/checkpoint.yaml",
            "misconception_target": m["misconception_target"],
            "sidequests": sq_by_parent.get(m["id"], {}),
            "depends_on": m.get("depends_on", []),
        })

    (out / "course.yaml").write_text(yaml.dump(manifest, default_flow_style=False))
    (out / "grader").mkdir(exist_ok=True)
    (out / "grader" / "sources.yaml").write_text(
        yaml.dump(harvest, default_flow_style=False))
    (out / "portfolio").mkdir(exist_ok=True)
    (out / "portfolio" / "state.yaml").write_text(
        yaml.dump({"entries": []}, default_flow_style=False))


def self_test(course_dir: Path) -> list[str]:
    """Structural consistency check. A failing course does not ship."""
    problems = []
    course_dir = Path(course_dir)
    manifest_path = course_dir / "course.yaml"
    if not manifest_path.exists():
        return ["course.yaml missing"]
    manifest = yaml.safe_load(manifest_path.read_text())

    for m in manifest.get("milestones", []):
        m_dir = course_dir / m["id"]
        if not (m_dir / "LESSON.md").exists():
            problems.append(f"{m['id']}: LESSON.md missing")
        cp = m_dir / "checkpoint.yaml"
        if not cp.exists():
            problems.append(f"{m['id']}: checkpoint.yaml missing")
        else:
            c = yaml.safe_load(cp.read_text())
            for field in ("grader_type", "misconception_target", "core_concepts"):
                if not c.get(field):
                    problems.append(f"{m['id']}: checkpoint missing {field}")
        for sq_path in (m.get("sidequests") or {}).values():
            if not (course_dir / "sidequests" / sq_path).exists():
                problems.append(f"{m['id']}: sidequest {sq_path} referenced but missing")
    return problems
