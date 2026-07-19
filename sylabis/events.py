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
#   compile.requested   {topic, learner_profile_hash}
#                       (hash only — NEVER the raw learner profile)
#   llm.usage           {stage, model, input_tokens, output_tokens,
#                        cost_usd, retries}
#                       (per-stage cost log; zeroed tokens in mock mode)
#   compile.completed   {milestone_count, viability_pct, grader_mode,
#                        okf_conformant}
#   milestone.started   {milestone_id}          (emitted by MCP get_lesson)
#   milestone.graded    {milestone_id, grade, passed, attempt, flags,
#                        hours_actual, hours_estimated}
#   path.decision       {signal, action, milestone_id}
#   sidequest.unlocked  {sidequest_id, trigger} (emitted by path actuation)
#   harvest.dropped     {source_id, locator, locator_kind, verification}
#                       (unverifiable locator dropped before sequencing)
#   compile.warning     {stage, reason, ...} (reasons: verification_drop_floor
#                       {total_sources, unverifiable}, milestone_source_floor
#                       {milestone_id, missing_source_ids})
#   course.published    {dest, license, author, sylabis_version}
#   remedial.injected   {remedial_id, parent_milestone_id, concept}
#   course.completed    {total_hours, milestones_passed}
