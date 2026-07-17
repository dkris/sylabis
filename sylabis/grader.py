"""
Grader. Tier 1 deterministic, Tier 2 claim audit, Tier 3 rubric
(exemplar-calibrated LLM for non-technical, executable scripts for
technical), explain-back always caps.
Writes grade result to <milestone>/grade.yaml and appends events.
The grader is the learner-facing quality ceiling: when in doubt it fails
with specific actionable feedback rather than passing with vague approval.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

from . import events
from . import okf
from .llm import LLM, parse_json
from . import prompts

SCRIPT_TIMEOUT = 300  # seconds per rubric script


def grade(course_dir: Path, milestone_id: str, llm: LLM,
          hours_actual: float | None = None,
          skip_tier3: bool = False) -> dict:
    course_dir = Path(course_dir)
    m_dir = course_dir / milestone_id
    checkpoint = yaml.safe_load((m_dir / "checkpoint.yaml").read_text())
    attempt = _next_attempt(m_dir)

    result = {"milestone_id": milestone_id, "attempt": attempt,
              "grade": 0.0, "passed": False, "failure_flags": [],
              "hours_actual": hours_actual,
              "graded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # ---- Tier 1: structural (deterministic, blocking) ----
    missing = [f for f in checkpoint["structural"]["required_files"]
               if not (m_dir / f).exists()]
    if missing:
        result["failure_flags"] = [f"missing_file:{f}" for f in missing]
        result["feedback"] = ("Structural check failed. Missing: "
                              + ", ".join(missing))
        _write(m_dir, course_dir, result, checkpoint)
        return result
    result["tier_1_passed"] = True

    # ---- Tier 2: claim audit (blocking). Skipped for reflection-only
    # checkpoints (remedials) that have no artifact to audit. ----
    if checkpoint["grader_type"] == "three_tier" and \
            "artifact.md" in checkpoint["structural"]["required_files"]:
        artifact = (m_dir / "artifact.md").read_text()
        audit = parse_json(llm.call(f"audit_{milestone_id}",
                                    prompts.CLAIM_AUDIT_SYSTEM, artifact))
        result["claim_audit"] = audit["summary"]
        result["failure_flags"].extend(audit["summary"]["flags"])
        if audit["summary"]["blocking"]:
            failed = [c for c in audit["claims"] if c["result"] == "fail"]
            result["feedback"] = "Claim audit failed:\n" + "\n".join(
                f'  - "{c["text"][:80]}" [{c["flag"]}] {c["feedback"]}'
                for c in failed)
            _write(m_dir, course_dir, result, checkpoint)
            return result
        result["tier_2_passed"] = True

    # ---- Tier 3 / rubric score ----
    base_score = 0.85  # conservative default when no rubric signal exists
    tier3_mode = "default"

    scripts = (checkpoint.get("rubric") or {}).get("scripts") or []
    if checkpoint["grader_type"] in ("executable", "hybrid") and scripts:
        script_score, script_flags, script_notes = _run_rubric_scripts(
            m_dir, scripts)
        result["rubric_scripts"] = script_notes
        result["failure_flags"].extend(script_flags)
        base_score = script_score if script_score is not None else 0.0
        tier3_mode = "rubric_scripts"
    elif checkpoint["grader_type"] == "three_tier" and "claim_audit" in result:
        # fallback while the exemplar set is unseeded
        s = result["claim_audit"]
        base_score = 0.6 + 0.4 * (s["passed"] / max(s["total"], 1))
        tier3_mode = "claim_audit_derived"

    t3cfg = checkpoint.get("tier_3_rubric") or {}
    if (t3cfg.get("enabled") and not skip_tier3
            and checkpoint["grader_type"] in ("three_tier", "hybrid")):
        artifact_path = m_dir / "artifact.md"
        t3 = grade_tier3(course_dir, milestone_id, checkpoint,
                         artifact_path.read_text()
                         if artifact_path.exists() else "", llm)
        if t3 is None:
            result["failure_flags"].append("tier3_enabled_but_unseeded")
        else:
            result["tier_3"] = t3
            base_score = t3["overall"]
            tier3_mode = "exemplar_rubric"
    result["tier_3_mode"] = tier3_mode

    # ---- Explain-back (always runs; can cap the grade) ----
    reflection = (m_dir / "reflection.md").read_text()
    eb = parse_json(llm.call(
        f"explain_{milestone_id}", prompts.EXPLAIN_BACK_SYSTEM,
        f"Misconception target: {checkpoint['misconception_target']}\n"
        f"Core concepts: {checkpoint['core_concepts']}\n"
        f"Reflection:\n{reflection}"))
    result["explain_back"] = eb["probes"]
    grade_val = base_score
    if eb.get("grade_cap"):
        grade_val = min(grade_val, eb["grade_cap"])
        result["failure_flags"].append("misconception_detected")
        result["weakest_concept"] = next(
            (p["concept"] for p in eb["probes"]
             if p["verdict"] == "misconception"), "")

    result["grade"] = round(grade_val, 3)
    result["passed"] = grade_val >= checkpoint.get("pass_threshold", 0.75)
    result["feedback"] = _feedback(result, eb)
    if result["passed"]:
        result["verified_claims"] = [
            f"Completed {checkpoint['artifact_type']} artifact for "
            f"{milestone_id}, grade {result['grade']:.0%}, "
            f"attempt {attempt}"]
        _update_portfolio(course_dir, result, checkpoint)
        okf.emit_claim_doc(course_dir, checkpoint, result)
        okf.emit_portfolio_index(course_dir)
    _write(m_dir, course_dir, result, checkpoint)
    if result["passed"]:
        _maybe_complete_course(course_dir)
    return result


def grade_tier3(course_dir: Path, milestone_id: str, checkpoint: dict,
                artifact: str, llm: LLM) -> dict | None:
    """Exemplar-calibrated rubric scoring. Returns None when the exemplar
    set is unseeded — the caller falls back rather than trusting an
    uncalibrated judgment."""
    rubric = checkpoint["tier_3_rubric"]
    exemplars = _load_exemplars(Path(course_dir), rubric)
    if not exemplars:
        return None
    ex_txt = "\n\n".join(
        f"--- exemplar [{e['category']}] score={e['score']}\n"
        f"grader reasoning: {e['reasoning']}\n{e['text'][:1500]}"
        for e in exemplars)
    return parse_json(llm.call(
        f"tier3_{milestone_id}", prompts.TIER3_RUBRIC_SYSTEM,
        f"Rubric dimensions: {rubric['dimensions']}\n\n"
        f"Annotated exemplars:\n{ex_txt}\n\n"
        f"Artifact to score:\n{artifact}"))


def _load_exemplars(course_dir: Path, rubric: dict) -> list[dict]:
    """Exemplars are human-annotated .md files with frontmatter:
    category: strong|adequate|weak, score: 0-1, reasoning: why."""
    ex_dir = course_dir / rubric.get("exemplars_dir", "")
    exemplars = []
    if rubric.get("exemplars_dir") and ex_dir.exists():
        for p in sorted(ex_dir.glob("*.md")):
            meta, body = okf.parse_doc(p)
            if meta and meta.get("category") in ("strong", "adequate", "weak"):
                exemplars.append({"category": meta["category"],
                                  "score": meta.get("score"),
                                  "reasoning": meta.get("reasoning", ""),
                                  "text": body.strip()})
    return exemplars


def _run_rubric_scripts(m_dir: Path, scripts: list[str]
                        ) -> tuple[float | None, list[str], list[str]]:
    """Run each rubric script in the milestone dir; each prints one JSON
    object {"score": 0-1, "metrics": {...}, "notes": ""} to stdout.
    Returns (mean score | None if any failed, failure flags, notes)."""
    scores, flags, notes = [], [], []
    for script in scripts:
        name = Path(script).name
        try:
            proc = subprocess.run(
                [sys.executable, script], cwd=m_dir,
                capture_output=True, text=True, timeout=SCRIPT_TIMEOUT)
        except subprocess.TimeoutExpired:
            flags.append(f"script_timeout:{name}")
            notes.append(f"{name}: timed out after {SCRIPT_TIMEOUT}s")
            continue
        if proc.returncode != 0:
            flags.append(f"script_error:{name}")
            notes.append(f"{name}: exit {proc.returncode} — "
                         f"{(proc.stderr or proc.stdout).strip()[-400:]}")
            continue
        try:
            out = json.loads(proc.stdout.strip().splitlines()[-1])
            score = float(out["score"])
            if not 0.0 <= score <= 1.0:
                raise ValueError(f"score {score} outside [0,1]")
        except (ValueError, KeyError, IndexError, json.JSONDecodeError) as e:
            flags.append(f"script_bad_output:{name}")
            notes.append(f"{name}: unparseable output ({e})")
            continue
        scores.append(score)
        notes.append(f"{name}: score {score:.2f} "
                     f"metrics={out.get('metrics', {})}")
    if len(scores) < len(scripts):
        return None, flags, notes  # any failed script fails the rubric
    return sum(scores) / len(scores), flags, notes


def _feedback(result: dict, eb: dict) -> str:
    lines = [f"Grade: {result['grade']:.0%} — "
             f"{'PASSED' if result['passed'] else 'NOT YET'}"]
    if result.get("tier_3"):
        lines.append(f"  Rubric ({result['tier_3_mode']}): "
                     f"{result['tier_3'].get('feedback', '')}")
        for d in result["tier_3"].get("dimensions", []):
            lines.append(f"    - {d['name']}: {d['score']:.2f} "
                         f"(nearest: {d.get('nearest_exemplar', '?')})")
    for note in result.get("rubric_scripts", []):
        lines.append(f"  [script] {note}")
    for p in eb["probes"]:
        mark = {"understood": "+", "surface": "~", "misconception": "!"}[p["verdict"]]
        lines.append(f"  [{mark}] {p['concept']}: {p['verdict']}")
        if p["verdict"] != "understood":
            lines.append(f"      Next question to sit with: {p['followup_question']}")
    return "\n".join(lines)


def _next_attempt(m_dir: Path) -> int:
    prior = m_dir / "grade.yaml"
    if prior.exists():
        return yaml.safe_load(prior.read_text()).get("attempt", 0) + 1
    return 1


def _write(m_dir: Path, course_dir: Path, result: dict,
           checkpoint: dict) -> None:
    """Persist one grading, then refresh everything derived from it. The
    order is load-bearing: the event lands before the report (so the
    report's history table includes this attempt) and okf.yaml is emitted
    LAST so the bundle manifest never goes stale after a grade. No git and
    no LLM calls in this module — CI commits grade state itself."""
    (m_dir / "grade.yaml").write_text(yaml.dump(result, default_flow_style=False))
    events.emit(course_dir, "milestone.graded", {
        **{k: result.get(k) for k in
           ("milestone_id", "grade", "passed", "attempt",
            "failure_flags", "hours_actual")},
        "hours_estimated": _estimated_hours(course_dir,
                                            result["milestone_id"])})
    okf.emit_grade_report(course_dir, checkpoint, result)
    if (course_dir / "course.yaml").exists():
        okf.emit_repo_readme(course_dir)   # the shareable landing page
        okf.emit_course_index(course_dir)  # progress shows in the index
        okf.emit_bundle_manifest(course_dir)


def _estimated_hours(course_dir: Path, milestone_id: str) -> float | None:
    mpath = Path(course_dir) / "course.yaml"
    if not mpath.exists():
        return None
    milestones = (yaml.safe_load(mpath.read_text()) or {}).get("milestones", [])
    m = next((m for m in milestones if m["id"] == milestone_id), None)
    return m.get("estimated_hours") if m else None


def _update_portfolio(course_dir: Path, result: dict, checkpoint: dict) -> None:
    state_path = course_dir / "portfolio" / "state.yaml"
    state = yaml.safe_load(state_path.read_text()) or {"entries": []}
    state["entries"].append({
        "milestone_id": result["milestone_id"],
        "artifact_type": checkpoint["artifact_type"],
        "verified_claims": result["verified_claims"],
        "graded_at": result["graded_at"]})
    state_path.write_text(yaml.dump(state, default_flow_style=False))


def _maybe_complete_course(course_dir: Path) -> None:
    """Emit course.completed exactly once, when every milestone has a
    passing grade."""
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
    grades = []
    for m in manifest.get("milestones", []):
        gpath = course_dir / m["id"] / "grade.yaml"
        if not gpath.exists():
            return
        g = yaml.safe_load(gpath.read_text()) or {}
        if not g.get("passed"):
            return
        grades.append(g)
    log = course_dir / "events.jsonl"
    if log.exists() and '"course.completed"' in log.read_text():
        return
    events.emit(course_dir, "course.completed", {
        "total_hours": sum(g.get("hours_actual") or 0 for g in grades),
        "milestones_passed": len(grades)})
