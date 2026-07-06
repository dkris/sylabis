"""
Event log — the graph seed. Append-only JSONL, one file per course.
This is the one-day change that decides whether you have a pathway
graph in three years or anecdotes. Every pipeline and grading action
emits an event. Schema is versioned; never mutate old events.
"""
import json
import time
import uuid
from pathlib import Path

SCHEMA_VERSION = 1


def emit(course_dir: Path, event_type: str, payload: dict) -> None:
    log = Path(course_dir) / "events.jsonl"
    event = {
        "schema": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "type": event_type,
        "payload": payload,
    }
    with open(log, "a") as f:
        f.write(json.dumps(event) + "\n")


# Event types (the future graph edges):
#   compile.requested   {topic, domain, learner_profile_hash}
#   compile.completed   {milestone_count, viability_pct, grader_mode}
#   milestone.started   {milestone_id}
#   milestone.graded    {milestone_id, grade, passed, attempt, flags,
#                        hours_actual, hours_estimated}
#   path.decision       {signal, action, milestone_id}
#   sidequest.unlocked  {sidequest_id, trigger}
#   course.completed    {total_hours, milestones_passed}
