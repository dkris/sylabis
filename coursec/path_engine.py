"""
Path engine. Deliberately dumb and legible — a rules table, not a model.
Reads grader output, emits decisions. If you can't explain an unlock
to a learner in one sentence, it doesn't belong here.
"""
from pathlib import Path

from . import events


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
        events.emit(course_dir, "path.decision",
                    {**d, "milestone_id": milestone.get("id", "")})
    return decisions
