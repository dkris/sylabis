"""
Compiler pipeline: intake -> harvest -> sequence -> emit -> self-test.
Output is a course directory matching the course-template schema.
"""
import time
from pathlib import Path

import yaml

from . import events
from . import okf
from .llm import LLM, parse_json
from . import prompts
from .verify import verify_sources


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

    # Stage 2 — Harvest (+ locator verification; flags, never blocks)
    print("[2/5] Source harvest...")
    harvest = parse_json(llm.call("harvest", prompts.HARVEST_SYSTEM,
                                  f"Compilation spec: {spec}"))
    vsum = verify_sources(harvest["sources"], enabled=not llm.mock)
    print(f"      {len(harvest['sources'])} sources — "
          f"{vsum['verified']} verified, {vsum['unverified']} unverified, "
          f"{vsum['flagged_search']} flagged search, "
          f"{vsum['skipped']} unchecked")

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
        "okf_conformant": True,  # gated: self-test failure never reaches here
    })
    print(f"\nCourse compiled: {out_dir}")
    return out_dir


def compile_remedial(course_dir: Path, parent_id: str, concept: str,
                     llm: LLM) -> str:
    """Mini compile: one supplementary micro-milestone targeting a specific
    misconception, inserted BEFORE the failed milestone so `next` serves it
    first. Reuses the lesson prompt with a tighter spec."""
    course_dir = Path(course_dir)
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
    idx, parent = next((i, m) for i, m in enumerate(manifest["milestones"])
                       if m["id"] == parent_id)
    n = 1 + sum(1 for m in manifest["milestones"]
                if m["id"].startswith(f"{parent_id}-r"))
    rid = f"{parent_id}-r{n}"
    milestone = {
        "id": rid,
        "title": f"Remedial — {concept}",
        "estimated_hours": 1,
        "artifact_type": "reflection",
        "artifact_spec": ("A short reflection demonstrating corrected "
                          f"understanding of {concept}"),
        "source_ids": [],
        "core_concepts": [concept],
        "misconception_target": parent.get("misconception_target", concept),
        "depends_on": parent.get("depends_on", []),
    }
    course_title = manifest["meta"]["title"]
    fm_block = okf.milestone_frontmatter(milestone, course_title)
    lesson = llm.call(
        f"lesson_{rid}", prompts.LESSON_SYSTEM,
        "Remedial micro-module (1 hour, reflection-only artifact). "
        f"Tighter spec: concept={concept!r}, "
        f"misconception={milestone['misconception_target']!r}, "
        f"milestone_parent={parent_id!r}\n"
        f"Milestone: {milestone}\n"
        f"OKF frontmatter block (prepend verbatim):\n{fm_block}")
    okf.emit_milestone_doc(course_dir, milestone, course_title, lesson)

    checkpoint = {
        "milestone_id": rid,
        "artifact_type": "reflection",
        "artifact_spec": milestone["artifact_spec"],
        "grader_type": "three_tier",
        # reflection-only: Tier 2 has no artifact to audit and skips itself
        "structural": {"required_files": ["reflection.md"]},
        "misconception_target": milestone["misconception_target"],
        "core_concepts": [concept],
        "pass_threshold": 0.75,
    }
    (course_dir / rid).mkdir(exist_ok=True)
    (course_dir / rid / "checkpoint.yaml").write_text(
        yaml.dump(checkpoint, default_flow_style=False))

    manifest["milestones"].insert(idx, {
        "id": rid, "title": milestone["title"],
        "estimated_hours": 1, "artifact_type": "reflection",
        "checkpoint": f"{rid}/checkpoint.yaml",
        "misconception_target": milestone["misconception_target"],
        "sidequests": {}, "depends_on": milestone["depends_on"],
    })
    (course_dir / "course.yaml").write_text(
        yaml.dump(manifest, default_flow_style=False))
    okf.emit_course_index(course_dir)
    okf.emit_bundle_manifest(course_dir)
    events.emit(course_dir, "remedial.injected",
                {"remedial_id": rid, "parent_milestone_id": parent_id,
                 "concept": concept})
    return rid


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

    course_title = spec["topic"]
    for m in seq["milestones"]:
        m_dir = out / m["id"]
        (m_dir / "starter").mkdir(parents=True, exist_ok=True)

        m_sources = [s for s in harvest["sources"] if s["id"] in m["source_ids"]]
        fm_block = okf.milestone_frontmatter(m, course_title)
        lesson = llm.call(f"lesson_{m['id']}", prompts.LESSON_SYSTEM,
                          f"Spec: {spec}\nMilestone: {m}\nSources: {m_sources}\n"
                          f"OKF frontmatter block (prepend verbatim):\n{fm_block}")
        # okf normalizes the frontmatter on receipt, so a model that
        # reformats the echoed block cannot break conformance.
        okf.emit_milestone_doc(out, m, course_title, lesson)

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
        if grader_mode in ("three_tier", "hybrid"):
            checkpoint["tier_3_rubric"] = {
                # Stays disabled until a human seeds the exemplar set
                # (15-20 annotated artifacts); the grader falls back to the
                # claim-audit-derived score while disabled.
                "enabled": False,
                "exemplars_dir": f"grader/exemplars/{m['id']}",
                "dimensions": _rubric_dimensions(m),
            }
        if grader_mode in ("executable", "hybrid"):
            checkpoint["rubric"] = {"scripts": ["starter/run_benchmark.py"]}
            (m_dir / "starter" / "run_benchmark.py").write_text(
                _BENCHMARK_TEMPLATE
                .replace("__MILESTONE__", m["id"])
                .replace("__ARTIFACT_SPEC__", m["artifact_spec"]))
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
    # sources.yaml is the machine-readable fallback for grader scripts;
    # knowledge/ is the canonical OKF representation. Duplication intended.
    (out / "grader").mkdir(exist_ok=True)
    (out / "grader" / "sources.yaml").write_text(
        yaml.dump(harvest, default_flow_style=False))
    (out / "portfolio").mkdir(exist_ok=True)
    (out / "portfolio" / "state.yaml").write_text(
        yaml.dump({"entries": []}, default_flow_style=False))

    # GitHub-as-UI: the bundle repo grades itself on push (P1). Requires
    # the learner's repo to define secret ANTHROPIC_API_KEY (and optionally
    # var COURSEC_GIT_URL); see comments inside the workflow.
    wf_dir = out / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "grade.yml").write_text(_GRADE_WORKFLOW)

    okf.emit_knowledge_bundle(out, harvest, course_title)
    okf.emit_portfolio_index(out)
    okf.emit_course_index(out)
    okf.emit_bundle_manifest(out)  # inventories every OKF doc; emit last


def _rubric_dimensions(m: dict) -> list[dict]:
    dims = [{"name": "artifact_fidelity",
             "description": f"Meets the artifact spec: {m['artifact_spec']}"}]
    dims += [{"name": c, "description": f"Demonstrates working command of {c}"}
             for c in m.get("core_concepts", [])]
    return dims


_GRADE_WORKFLOW = '''\
# coursec grader — emitted into every compiled course bundle.
# GitHub IS the UI: push artifact.md + reflection.md, get graded feedback
# as a commit (push) or a PR comment (pull request).
#
# One-time repo setup:
#   1. Settings -> Secrets: add ANTHROPIC_API_KEY
#   2. (optional) Settings -> Variables: COURSEC_GIT_URL to pin the
#      compiler source; defaults below.
name: grade

on:
  push:
    paths: ["*/artifact.md", "*/reflection.md"]
  pull_request:
    paths: ["*/artifact.md", "*/reflection.md"]

permissions:
  contents: write
  pull-requests: write

concurrency:
  group: grade-${{ github.ref }}
  cancel-in-progress: false

jobs:
  grade:
    runs-on: ubuntu-latest
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install coursec
        run: pip install "${{ vars.COURSEC_GIT_URL || 'git+https://github.com/dhruvakrishnan/coursec' }}"

      - name: Find changed milestones
        id: changed
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            BASE="${{ github.event.pull_request.base.sha }}"
          else
            BASE="${{ github.event.before }}"
          fi
          git cat-file -e "$BASE" 2>/dev/null || BASE=$(git hash-object -t tree /dev/null)
          MIDS=$(git diff --name-only "$BASE" HEAD -- '*/artifact.md' '*/reflection.md' \\
                 | cut -d/ -f1 | sort -u | tr '\\n' ' ')
          echo "mids=$MIDS" >> "$GITHUB_OUTPUT"

      - name: Grade changed milestones
        id: grade
        run: |
          : > /tmp/grade-report.md
          FAILED=0
          for MID in ${{ steps.changed.outputs.mids }}; do
            echo "## $MID" >> /tmp/grade-report.md
            echo '```'    >> /tmp/grade-report.md
            if ! coursec grade . "$MID" >> /tmp/grade-report.md 2>&1; then
              FAILED=1
            fi
            echo '```'    >> /tmp/grade-report.md
          done
          cat /tmp/grade-report.md
          echo "failed=$FAILED" >> "$GITHUB_OUTPUT"

      - name: Post feedback as PR comment
        if: github.event_name == 'pull_request'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh pr comment "${{ github.event.pull_request.number }}" --body-file /tmp/grade-report.md

      - name: Commit grade state
        if: github.event_name == 'push'
        run: |
          git config user.name "coursec-grader"
          git config user.email "grader@users.noreply.github.com"
          git add -A
          git commit -m "grade: ${{ steps.changed.outputs.mids }}" || echo "nothing to commit"
          git push

      - name: Fail the check when a milestone did not pass
        if: steps.grade.outputs.failed == '1'
        run: exit 1
'''

_BENCHMARK_TEMPLATE = '''\
"""
Rubric benchmark for __MILESTONE__. Fill in the TODOs, then run:

    python starter/run_benchmark.py

The grader runs this script in the milestone directory and reads ONE JSON
object from stdout:

    {"score": <float 0.0-1.0>, "metrics": {...}, "notes": "<optional>"}

Artifact spec: __ARTIFACT_SPEC__
"""
import json


def run() -> dict:
    metrics = {}
    # TODO: exercise your artifact and populate metrics
    # (e.g. tokens/sec, peak memory, accuracy on your eval set),
    # then derive a score in [0.0, 1.0] from them.
    raise NotImplementedError("fill in run() before grading")
    return {"score": 0.0, "metrics": metrics, "notes": ""}


if __name__ == "__main__":
    print(json.dumps(run()))
'''


def self_test(course_dir: Path) -> list[str]:
    """Structural consistency + OKF conformance. A failing course does not
    ship — no skip flag, ever."""
    problems = []
    course_dir = Path(course_dir)
    manifest_path = course_dir / "course.yaml"
    if not manifest_path.exists():
        return ["course.yaml missing"]
    manifest = yaml.safe_load(manifest_path.read_text())
    problems.extend(okf.conformance_problems(course_dir))

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
