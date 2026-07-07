"""
Path engine. Deliberately dumb and legible — a rules table, not a model.
decide() reads grader output and emits decisions; actuate() is the only
place those decisions change state (unlock sidequests, inject remedials).
If you can't explain an unlock to a learner in one sentence, it doesn't
belong here.
"""
import time
from pathlib import Path

import yaml

from . import events
from . import okf


def decide(course_dir: Path, milestone: dict, grade_result: dict) -> list[dict]:
    decisions = []
    grade = grade_result.get("grade", 0.0)
    attempt = grade_result.get("attempt", 1)
    flags = grade_result.get("failure_flags", [])
    hours_est = milestone.get("estimated_hours") or 0
    hours_act = grade_result.get("hours_actual") or hours_est

    if grade >= 0.9 and milestone.get("sidequests", {}).get("depth"):
        decisions.append({"signal": "grade_gte_90", "action": "unlock_depth_sidequest",
                          "target": milestone["sidequests"]["depth"]})

    if hours_est and hours_act <= hours_est * 0.7 and \
            milestone.get("sidequests", {}).get("frontier"):
        decisions.append({"signal": "finish_ahead_30pct",
                          "action": "unlock_frontier_sidequest",
                          "target": milestone["sidequests"]["frontier"]})

    if grade < 0.6 or attempt >= 2 and not grade_result.get("passed"):
        decisions.append({"signal": "grade_lt_60" if grade < 0.6 else "two_failed_attempts",
                          "action": "inject_remedial",
                          "target": grade_result.get("weakest_concept",
                                                     milestone.get("misconception_target", ""))})

    claim_flags = [f for f in flags if f in
                   ("overclaiming", "missing_n", "unsupported_causal",
                    "false_precision", "underpowered", "percentage_of_what")]
    if len(claim_flags) > 3:
        decisions.append({"signal": "claim_audit_failures_gt_3",
                          "action": "inject_claim_audit_module",
                          "target": "evidence-and-claims"})

    for d in decisions:
        d["milestone_id"] = milestone.get("id", "")
        events.emit(course_dir, "path.decision", d)
    return decisions


def actuate(course_dir: Path, decisions: list[dict], llm=None) -> list[str]:
    """Act on decisions. Returns human-readable lines of what happened.
    Remedial generation needs an LLM; pass llm=None to log-and-skip it."""
    from .compiler import compile_remedial

    course_dir = Path(course_dir)
    done = []
    index_dirty = False

    for d in decisions:
        if d["action"] in ("unlock_depth_sidequest", "unlock_frontier_sidequest"):
            sq_yaml = course_dir / "sidequests" / d["target"] / "sidequest.yaml"
            if not sq_yaml.exists():
                done.append(f"! sidequest {d['target']} referenced but not on disk")
                continue
            sq = yaml.safe_load(sq_yaml.read_text()) or {}
            if not sq.get("locked", True):
                continue  # already unlocked
            sq["locked"] = False
            sq["unlocked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            sq["unlock_trigger"] = d["signal"]
            sq_yaml.write_text(yaml.dump(sq, default_flow_style=False))
            events.emit(course_dir, "sidequest.unlocked",
                        {"sidequest_id": d["target"], "trigger": d["signal"]})
            index_dirty = True
            done.append(f"unlocked sidequest {d['target']} — {sq.get('hook', '')}")

        elif d["action"] in ("inject_remedial", "inject_claim_audit_module"):
            parent = d.get("milestone_id", "")
            if _pending_remedial(course_dir, parent):
                done.append(f"remedial for {parent} already pending — not adding another")
                continue
            if llm is None:
                done.append(f"! remedial for {d['target']!r} signaled but no LLM available")
                continue
            rid = compile_remedial(course_dir, parent, d["target"], llm)
            done.append(f"injected remedial {rid} targeting {d['target']!r}")

    if index_dirty:
        okf.emit_course_index(course_dir)
    return done


def _pending_remedial(course_dir: Path, parent_id: str) -> bool:
    """One unpassed remedial per milestone at a time — repeated failures
    should route back through it, not stack new modules."""
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
    for m in manifest.get("milestones", []):
        if m["id"].startswith(f"{parent_id}-r"):
            gpath = course_dir / m["id"] / "grade.yaml"
            if not gpath.exists() or \
                    not (yaml.safe_load(gpath.read_text()) or {}).get("passed"):
                return True
    return False
