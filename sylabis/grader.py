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

from . import __version__
from . import events
from . import journey
from . import okf
from . import prompts
from . import sandbox
from .errors import SandboxError
from .llm import LLM, stage_model

SCRIPT_TIMEOUT = 300  # seconds per rubric script

# Neutral score substituted for an unscored pass when the path engine
# needs a number (see path_engine_view): inside [0.6, 0.9) so it earns
# no grade-based unlock and triggers no grade-based remediation.
_UNSCORED_NEUTRAL = 0.75


def grade(course_dir: Path, milestone_id: str, llm: LLM,
          hours_actual: float | None = None,
          skip_tier3: bool = False,
          unsandboxed: bool = False) -> dict:
    """Grade one milestone. While grading, the LLM logs llm.usage cost
    events into the course's events.jsonl (WS1a); restored on exit so a
    shared LLM instance doesn't keep pointing at this course."""
    course_dir = Path(course_dir)
    prev_usage = llm.usage_dir
    llm.usage_dir = course_dir
    try:
        return _grade(course_dir, milestone_id, llm, hours_actual,
                      skip_tier3, unsandboxed)
    finally:
        llm.usage_dir = prev_usage


def path_engine_view(result: dict) -> dict:
    """The grade result as path_engine.decide() consumes it. decide() is
    deliberately a legible numeric rules table; an unscored result
    (grade None) substitutes a neutral number — threshold-level on pass
    (no unlock, no remediation), 0.0 on fail (routes to remediation) —
    so pathing stays deterministic without teaching the rules table
    about None. Never written to grade.yaml."""
    if result.get("grade") is not None:
        return result
    return {**result,
            "grade": _UNSCORED_NEUTRAL if result.get("passed") else 0.0}


def _stage_provenance(family: str) -> dict:
    """{model, prompt_version, sylabis_version} for one grader stage —
    the same traceability shape compiler stamps into course.yaml meta."""
    return {"model": stage_model(family),
            "prompt_version": prompts.prompt_version(family),
            "sylabis_version": __version__}


def _grade(course_dir: Path, milestone_id: str, llm: LLM,
           hours_actual: float | None, skip_tier3: bool,
           unsandboxed: bool) -> dict:
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

    # ---- Tier 2: claim audit (blocking). Skipped for reflection-only
    # checkpoints (remedials) that have no artifact to audit. ----
    if checkpoint["grader_type"] == "three_tier" and \
            "artifact.md" in checkpoint["structural"]["required_files"]:
        artifact = (m_dir / "artifact.md").read_text()
        audit = llm.call_json(f"audit_{milestone_id}",
                              prompts.CLAIM_AUDIT_SYSTEM, artifact)
        result.setdefault("provenance", {})["audit"] = _stage_provenance("audit")
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
    base_score: float | None = 0.85  # conservative default (reflection-only)
    tier3_mode = "default"

    scripts = (checkpoint.get("rubric") or {}).get("scripts") or []
    if checkpoint["grader_type"] in ("executable", "hybrid") and scripts:
        runner = _script_runner(course_dir, scripts, unsandboxed)
        script_score, script_flags, script_notes = _run_rubric_scripts(
            course_dir, m_dir, scripts, runner)
        result["rubric_scripts"] = script_notes
        result["failure_flags"].extend(script_flags)
        base_score = script_score if script_score is not None else 0.0
        tier3_mode = "rubric_scripts"
    elif checkpoint["grader_type"] == "three_tier" and "claim_audit" in result:
        # Fallback while the exemplar set is unseeded. base = pass_ratio:
        # the audit pass ratio is the ONLY quality signal available here,
        # so the pre-explain base reaches the 0.75 pass threshold only
        # when >=75% of claims survive the audit. The old 0.6+0.4*ratio
        # floor let a ~38%-claims-passing artifact score 0.75 (WS4.3b).
        s = result["claim_audit"]
        base_score = s["passed"] / max(s["total"], 1)
        tier3_mode = "claim_audit_derived"
    elif checkpoint["grader_type"] in ("executable", "hybrid"):
        # WS4.3a: an executable checkpoint that declares no rubric
        # scripts has NO Tier-3 signal. Never fabricate a score —
        # pass/fail is decided on Tiers 1-2 (plus the explain-back
        # misconception gate) and the record is flagged unscored.
        base_score = None
        tier3_mode = "unscored"

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
            result.setdefault("provenance", {})["tier3"] = \
                _stage_provenance("tier3")
            base_score = t3["overall"]
            tier3_mode = "exemplar_rubric"
    result["tier_3_mode"] = tier3_mode

    # ---- Explain-back (always runs; can cap the grade) ----
    reflection = (m_dir / "reflection.md").read_text()
    eb = llm.call_json(
        f"explain_{milestone_id}", prompts.EXPLAIN_BACK_SYSTEM,
        f"Misconception target: {checkpoint['misconception_target']}\n"
        f"Core concepts: {checkpoint['core_concepts']}\n"
        f"Reflection:\n{reflection}")
    result.setdefault("provenance", {})["explain"] = _stage_provenance("explain")
    result["explain_back"] = eb["probes"]
    threshold = checkpoint.get("pass_threshold", 0.75)
    if eb.get("grade_cap"):
        result["failure_flags"].append("misconception_detected")
        result["weakest_concept"] = next(
            (p["concept"] for p in eb["probes"]
             if p["verdict"] == "misconception"), "")

    if base_score is None:
        # Unscored: Tiers 1-2 already passed to get here; the explain-back
        # cap still beats that pass (a detected misconception blocks).
        result["unscored"] = True
        result["grade"] = None
        result["passed"] = not (eb.get("grade_cap")
                                and eb["grade_cap"] < threshold)
    else:
        grade_val = base_score
        if eb.get("grade_cap"):
            grade_val = min(grade_val, eb["grade_cap"])
        result["grade"] = round(grade_val, 3)
        result["passed"] = grade_val >= threshold
    result["feedback"] = _feedback(result, eb)
    if result["passed"]:
        if result["grade"] is None:
            # An unscored pass advances the course but mints no scored
            # portfolio claim — okf claim docs carry a numeric grade and
            # there is none to put there honestly.
            result["verified_claims"] = [
                f"Completed {checkpoint['artifact_type']} artifact for "
                f"{milestone_id} (unscored: Tiers 1-2 + explain-back), "
                f"attempt {attempt}"]
        else:
            result["verified_claims"] = [
                f"Completed {checkpoint['artifact_type']} artifact for "
                f"{milestone_id}, grade {result['grade']:.0%}, "
                f"attempt {attempt}"]
            _update_portfolio(course_dir, result, checkpoint)
            okf.emit_claim_doc(course_dir, checkpoint, result)
            okf.emit_portfolio_index(course_dir)
    _write(m_dir, course_dir, result)
    if result["passed"]:
        _maybe_complete_course(course_dir)
    return result


def _script_runner(course_dir: Path, scripts: list[str],
                   unsandboxed: bool) -> sandbox.SandboxRunner:
    """Build the sandbox runner for this bundle's rubric scripts, gating
    ATTACHED bundles on recorded consent: the first grade of an attached
    course that declares scripts must be explicitly approved once per
    bundle (sandbox.record_trust) before anything executes."""
    info = journey.attach_info(course_dir)
    origin = (info or {}).get("source") or str(course_dir)
    if info and not sandbox.trusted(course_dir):
        raise sandbox.ConsentRequired(origin, scripts)
    return sandbox.SandboxRunner(origin=origin, unsandboxed=unsandboxed)


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
    return llm.call_json(
        f"tier3_{milestone_id}", prompts.TIER3_RUBRIC_SYSTEM,
        f"Rubric dimensions: {rubric['dimensions']}\n\n"
        f"Annotated exemplars:\n{ex_txt}\n\n"
        f"Artifact to score:\n{artifact}")


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


def _run_rubric_scripts(course_dir: Path, m_dir: Path, scripts: list[str],
                        runner: sandbox.SandboxRunner
                        ) -> tuple[float | None, list[str], list[str]]:
    """Run each rubric script in the milestone dir; each prints one JSON
    object {"score": 0-1, "metrics": {...}, "notes": ""} to stdout.
    Returns (mean score | None if any failed, failure flags, notes).

    Script paths are bundle-declared (attacker-controlled once attach
    is public): absolute paths, '../' escapes past the course bundle,
    and nonexistent targets are refused with a typed error BEFORE
    anything executes — the same resolve+is_relative_to containment
    tools._read uses. Execution goes through the SandboxRunner (scrubbed
    env always; bwrap/nsjail when available)."""
    scores, flags, notes = [], [], []
    croot = Path(course_dir).resolve()
    for script in scripts:
        name = Path(script).name
        if Path(script).is_absolute():
            raise SandboxError(
                f"rubric script {script!r} is an absolute path — refused")
        target = (Path(m_dir) / script).resolve()
        if not target.is_relative_to(croot):
            raise SandboxError(
                f"rubric script {script!r} escapes the course bundle — refused")
        if not target.is_file():
            raise SandboxError(
                f"rubric script {script!r} does not exist in the bundle")
        try:
            proc = runner.run([sys.executable, str(target)], cwd=m_dir,
                              timeout=SCRIPT_TIMEOUT)
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
    verdict = "PASSED" if result["passed"] else "NOT YET"
    if result.get("grade") is None:
        lines = [f"Grade: unscored — {verdict} (no rubric scripts declared; "
                 f"pass/fail from Tiers 1-2 + explain-back only)"]
    else:
        lines = [f"Grade: {result['grade']:.0%} — {verdict}"]
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


def _write(m_dir: Path, course_dir: Path, result: dict) -> None:
    # Unscored results drop the grade key from grade.yaml entirely
    # (never `grade: null`): downstream renderers format grades
    # numerically and their g.get("grade", 0) defaults only fire on a
    # missing key. The 'unscored: true' + tier_3_mode flags carry the
    # semantics.
    record = {k: v for k, v in result.items()
              if not (k == "grade" and v is None)}
    (m_dir / "grade.yaml").write_text(
        yaml.dump(record, default_flow_style=False))
    events.emit(course_dir, "milestone.graded", {
        k: result.get(k) for k in
        ("milestone_id", "grade", "passed", "attempt",
         "failure_flags", "hours_actual")})
    if (course_dir / "course.yaml").exists():
        okf.emit_course_index(course_dir)  # progress shows in the index


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
