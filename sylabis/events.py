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
#   compile.completed   {milestone_count, viability_pct, grader_mode,
#                        okf_conformant}
#   repo.initialized    {branch}                (bundle became a git repo)
#   milestone.started   {milestone_id}          (emitted by MCP get_lesson)
#   milestone.graded    {milestone_id, grade, passed, attempt,
#                        failure_flags, hours_actual, hours_estimated}
#   path.decision       {signal, action, milestone_id}
#   sidequest.unlocked  {sidequest_id, trigger} (emitted by path actuation)
#   remedial.injected   {remedial_id, parent_milestone_id, concept}
#   course.completed    {total_hours, milestones_passed}
#   course.published    {remote}                (sy publish set/pushed origin)
#   course.synced       {remote, pulled, pushed} (only when something moved)
