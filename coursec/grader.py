"""
Grader. Tier 1 deterministic, Tier 2/3 via LLM, explain-back always.
Writes grade result to <milestone>/grade.yaml and appends events.
"""
import time
from pathlib import Path

import yaml

from . import events
from .llm import LLM, parse_json
from . import prompts


def grade(course_dir: Path, milestone_id: str, llm: LLM,
          hours_actual: float | None = None) -> dict:
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
        _write(m_dir, course_dir, result)
        return result
    result["tier_1_passed"] = True

    # ---- Tier 2: claim audit (three_tier mode only, blocking) ----
    if checkpoint["grader_type"] == "three_tier":
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
            _write(m_dir, course_dir, result)
            return result
        result["tier_2_passed"] = True

    # ---- Tier 3 / rubric score ----
    # Prototype: technical rubric scripts are course-specific; where present
    # run them, else score from claim audit pass-rate or default to explain-back.
    base_score = 0.85  # placeholder for rubric/exemplar scoring in MVP
    if checkpoint["grader_type"] == "three_tier" and "claim_audit" in result:
        s = result["claim_audit"]
        base_score = 0.6 + 0.4 * (s["passed"] / max(s["total"], 1))

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
    _write(m_dir, course_dir, result)
    return result


def _feedback(result: dict, eb: dict) -> str:
    lines = [f"Grade: {result['grade']:.0%} — "
             f"{'PASSED' if result['passed'] else 'NOT YET'}"]
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


def _write(m_dir: Path, course_dir: Path, result: dict) -> None:
    (m_dir / "grade.yaml").write_text(yaml.dump(result, default_flow_style=False))
    events.emit(course_dir, "milestone.graded", {
        k: result.get(k) for k in
        ("milestone_id", "grade", "passed", "attempt",
         "failure_flags", "hours_actual")})


def _update_portfolio(course_dir: Path, result: dict, checkpoint: dict) -> None:
    state_path = course_dir / "portfolio" / "state.yaml"
    state = yaml.safe_load(state_path.read_text()) or {"entries": []}
    state["entries"].append({
        "milestone_id": result["milestone_id"],
        "artifact_type": checkpoint["artifact_type"],
        "verified_claims": result["verified_claims"],
        "graded_at": result["graded_at"]})
    state_path.write_text(yaml.dump(state, default_flow_style=False))
