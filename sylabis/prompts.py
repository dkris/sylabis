"""
Pipeline stage prompts. These ARE the compiler — the Python around
them is plumbing. Version these like code, because they are code.

PROMPT_VERSIONS is the machine-readable version of the per-prompt
comment headers below. Bump the entry whenever you change its prompt —
the compiler stamps {model, prompt_version, sylabis_version} per stage
into course.yaml meta (WS1a.6), and the grader wave stamps the same
shape into grade.yaml. Keys are stage FAMILIES (see llm.stage_family):
"lesson_00-intro" resolves to "lesson".

WS4.1: every JSON-emitting stage also carries a JSON Schema in
STAGE_OUTPUT_SCHEMAS (bottom of this file). llm.call() sends it to the
API as a structured-outputs constraint (output_config.format), so on
live calls a schema violation is impossible by construction. The
prompt + schema pair IS the stage — change one, review the other, and
bump the PROMPT_VERSIONS entry for either change.
"""

PROMPT_VERSIONS = {
    "guide": "1",
    "intake": "1",
    "harvest": "2",
    "sequence": "1",
    "lesson": "2",
    "audit": "1",
    "tier3": "1",
    "explain": "1",
}


def prompt_version(stage: str) -> str:
    """Version for a stage name or family; 'unversioned' when unknown."""
    return PROMPT_VERSIONS.get(stage.split("_", 1)[0], "unversioned")

# v1 (2026-07-11): the agent harness persona. The tools do the mechanics;
# this prompt only sets how the guide behaves between them.
GUIDE_SYSTEM = """You are Sy, a learning guide. One learner, one
journey of courses, and tools that compile courses, serve lessons, grade
real artifacts, and map verified knowledge. You drive the loop so the
learner only has to learn.

Rules:
- Orient before advising: call `journey` at the start of a session.
- One next action at a time. Never present a menu of milestones.
- The work is the learner's. Discuss, probe, point at lessons and
  sources — but never write or improve their artifact or reflection.
  submit_work only with text the learner gave you verbatim.
- Grades come only from submit_work. Never predict or promise one.
- Connect: before a new milestone or course, check knowledge_map and
  say how the new material builds on a concept they already verified.
- When they want to learn something new, start_course with their topic
  as they said it — the compiler handles the rest.
- Failed grades are path signals, not verdicts: relay the feedback,
  name the one thing to fix, and point back at the lesson or a source.
- Be brief. The learner's time belongs to the milestone, not the chat."""


INTAKE_SYSTEM = """You are the intake stage of a course compiler.
Given a topic prompt and learner profile, output a JSON compilation spec.
Output ONLY JSON, no prose, no markdown fences.

Schema:
{
  "topic": "normalized topic title",
  "domain": "technical" | "non-technical" | "hybrid",
  "target_artifact": "the concrete thing the learner will have at the end",
  "assumed_knowledge": ["concepts the learner has"],
  "constraints": {"weekly_hours": N, "hardware": "..." },
  "viability": {
    "verifiable_skeleton_pct": 0-100,
    "grader_mode": "executable" | "three_tier" | "hybrid",
    "verdict": "proceed" | "decline",
    "notes": "one sentence"
  }
}
For technical topics verifiable_skeleton_pct is typically 80+.
For knowledge-work topics estimate honestly. Below 20 → verdict: decline."""

# v2 (2026-07-06): added okf_description — one-line summary for the OKF
# source document. Fixtures updated to match.
HARVEST_SYSTEM = """You are the source harvest stage of a course compiler.
Given a compilation spec, list the primary sources a course should compile from.
Use only sources you are confident exist: canonical papers (with arXiv IDs),
official documentation (with real URLs), foundational books.
Do NOT invent URLs. If unsure of a URL, give the source name and where to find it.
Output ONLY JSON:
{
  "sources": [
    {"id": "short-id", "title": "", "author": "", "year": 0,
     "type": "paper|docs|book|guide|counter_example",
     "locator": "arxiv ID, URL, or 'search: <terms>'",
     "authority": 0.0-1.0,
     "what_learner_needs": "specific section or concept",
     "okf_description": "one sentence (<=120 chars) saying what this source is",
     "freshness_class": "fast|medium|stable|evergreen"}
  ]
}"""

SEQUENCE_SYSTEM = """You are the sequencing stage of a course compiler.
Given a compilation spec and harvested sources, produce 4-6 milestones.
Hard constraints:
1. Every milestone ends in a buildable artifact (never "understand X").
2. Theory appears at most one milestone before it is used.
3. Milestone 0 must be completable in under 4 hours (momentum matters).
4. Each milestone names the ONE misconception most likely at that stage.
Output ONLY JSON:
{
  "milestones": [
    {"id": "00-slug", "title": "", "estimated_hours": N,
     "artifact_type": "", "artifact_spec": "one precise sentence",
     "source_ids": ["ids from harvest"],
     "core_concepts": ["concepts introduced here"],
     "misconception_target": "the misconception the grader must probe",
     "depends_on": []}
  ],
  "sidequests": [
    {"id": "", "type": "depth|frontier", "parent": "milestone id",
     "title": "", "hook": "one compelling sentence shown at unlock"}
  ]
}"""

# v2 (2026-07-06): lesson input now includes an OKF frontmatter block to
# prepend verbatim. The compiler normalizes the block on receipt, so a
# reformatted echo cannot break conformance.
LESSON_SYSTEM = """You write LESSON.md files for a compiled course.
Given the compilation spec, one milestone, its sources, and an OKF
frontmatter block, write the lesson.
Begin the document with the provided OKF frontmatter block VERBATIM —
do not reorder, requote, or reformat it.
Structure after the frontmatter: Title, time estimate, artifact statement,
Context (why now), Learn from (sources with what-you-need annotations),
Do (numbered concrete steps), Artifact spec (checkable requirements),
Reflection prompt (target the named misconception), What the grader checks.
Be direct. No filler. Every 'Do' step has a concrete output.
Output raw markdown only."""

CLAIM_AUDIT_SYSTEM = """You are a structured claim auditor — Tier 2 of a
three-tier grader. You do NOT judge writing quality. You verify that every
factual claim matches its evidence strength.

For every claim: extract exact text, classify
(descriptive|comparative|causal|predictive|evaluative), locate evidence,
apply the rule, emit pass/fail with flag.

Flags: overclaiming, underpowered, missing_n, false_precision,
unsupported_causal, percentage_of_what.

Rules:
- descriptive: requires n; inferential requires uncertainty bounds
- comparative: requires n per group and effect size or practical significance
- causal: requires hedge language; "proves/shows/confirms" without
  experimental design = unsupported_causal
- predictive: requires explicit uncertainty acknowledgment
- evaluative: requires stated criteria

Output ONLY JSON:
{"claims": [{"text": "", "type": "", "evidence": "", "result": "pass|fail",
             "flag": null, "feedback": ""}],
 "summary": {"total": N, "passed": N, "failed": N, "flags": [],
             "blocking": true|false}}"""

# v1 (2026-07-06): Tier 3 exemplar-calibrated rubric scoring. Requires a
# seeded exemplar set; the grader skips Tier 3 when exemplars are absent.
TIER3_RUBRIC_SYSTEM = """You are Tier 3 of a three-tier grader:
exemplar-calibrated rubric scoring. You receive rubric dimensions, annotated
exemplars (strong / adequate / weak, each with a score and the grader's
written reasoning), and a submitted artifact.

Calibrate against the exemplars, not absolute taste: for each dimension,
place the artifact relative to its nearest exemplars and score accordingly.
Do not re-audit factual claims (Tier 2 already did) and do not reward length.

Output ONLY JSON:
{"dimensions": [{"name": "", "score": 0.0-1.0,
                 "nearest_exemplar": "strong|adequate|weak",
                 "rationale": "one sentence"}],
 "overall": 0.0-1.0,
 "feedback": "the 2-3 most actionable improvements, specific to this artifact"}
overall is a holistic judgment, not an average of dimensions."""

EXPLAIN_BACK_SYSTEM = """You interrogate a learner's reflection for
misconceptions that passing rubric scripts would not catch.
Given the milestone's misconception target and the reflection, ask yourself
whether the reflection demonstrates understanding, surface familiarity,
or the misconception itself.
Output ONLY JSON:
{"probes": [{"concept": "", "verdict": "understood|surface|misconception",
             "evidence": "quote or paraphrase from reflection",
             "followup_question": "the question you would ask next"}],
 "grade_cap": null | 0.7}
Set grade_cap to 0.7 if any core concept shows a misconception."""


# ------------------------------------------------------------- JSON schemas
# WS4.1 structured outputs. One JSON Schema per JSON-emitting stage family,
# living next to its prompt because the pair is the stage. llm.call()
# requests these via the API's output_config.format mechanism; llm.py's
# STAGE_SCHEMAS required-keys check stays as the belt-and-suspenders
# fallback that mock mode and the one-round repair loop validate against.
# Raw-markdown stages (lesson) and the conversational guide have no entry.

_CLAIM_FLAGS = ["overclaiming", "underpowered", "missing_n",
                "false_precision", "unsupported_causal", "percentage_of_what"]

INTAKE_SCHEMA = {
    "type": "object",
    "required": ["topic", "domain", "target_artifact", "assumed_knowledge",
                 "constraints", "viability"],
    "additionalProperties": False,
    "properties": {
        "topic": {"type": "string"},
        "domain": {"enum": ["technical", "non-technical", "hybrid"]},
        "target_artifact": {"type": "string"},
        "assumed_knowledge": {"type": "array", "items": {"type": "string"}},
        "constraints": {"type": "object"},
        "viability": {
            "type": "object",
            "required": ["verifiable_skeleton_pct", "grader_mode",
                         "verdict", "notes"],
            "additionalProperties": False,
            "properties": {
                "verifiable_skeleton_pct": {"type": "number",
                                            "minimum": 0, "maximum": 100},
                "grader_mode": {"enum": ["executable", "three_tier",
                                         "hybrid"]},
                "verdict": {"enum": ["proceed", "decline"]},
                "notes": {"type": "string"},
            },
        },
    },
}

HARVEST_SCHEMA = {
    "type": "object",
    "required": ["sources"],
    "additionalProperties": False,
    "properties": {
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "author", "year", "type",
                             "locator", "authority", "what_learner_needs",
                             "okf_description", "freshness_class"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "author": {"type": "string"},
                    "year": {"type": "integer"},
                    "type": {"enum": ["paper", "docs", "book", "guide",
                                      "counter_example"]},
                    "locator": {"type": "string"},
                    "authority": {"type": "number",
                                  "minimum": 0, "maximum": 1},
                    "what_learner_needs": {"type": "string"},
                    "okf_description": {"type": "string", "maxLength": 120},
                    "freshness_class": {"enum": ["fast", "medium", "stable",
                                                 "evergreen"]},
                },
            },
        },
    },
}

SEQUENCE_SCHEMA = {
    "type": "object",
    "required": ["milestones", "sidequests"],
    "additionalProperties": False,
    "properties": {
        "milestones": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "title", "estimated_hours",
                             "artifact_type", "artifact_spec", "source_ids",
                             "core_concepts", "misconception_target",
                             "depends_on"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "estimated_hours": {"type": "number", "minimum": 0},
                    "artifact_type": {"type": "string"},
                    "artifact_spec": {"type": "string"},
                    "source_ids": {"type": "array",
                                   "items": {"type": "string"}},
                    "core_concepts": {"type": "array",
                                      "items": {"type": "string"}},
                    "misconception_target": {"type": "string"},
                    "depends_on": {"type": "array",
                                   "items": {"type": "string"}},
                },
            },
        },
        "sidequests": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "type", "parent", "title", "hook"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "type": {"enum": ["depth", "frontier"]},
                    "parent": {"type": "string"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                },
            },
        },
    },
}

CLAIM_AUDIT_SCHEMA = {
    "type": "object",
    "required": ["claims", "summary"],
    "additionalProperties": False,
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "type", "evidence", "result", "flag",
                             "feedback"],
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "type": {"enum": ["descriptive", "comparative", "causal",
                                      "predictive", "evaluative"]},
                    "evidence": {"type": "string"},
                    "result": {"enum": ["pass", "fail"]},
                    "flag": {"anyOf": [{"type": "null"},
                                       {"enum": _CLAIM_FLAGS}]},
                    "feedback": {"type": "string"},
                },
            },
        },
        "summary": {
            "type": "object",
            "required": ["total", "passed", "failed", "flags", "blocking"],
            "additionalProperties": False,
            "properties": {
                "total": {"type": "integer"},
                "passed": {"type": "integer"},
                "failed": {"type": "integer"},
                "flags": {"type": "array", "items": {"type": "string"}},
                "blocking": {"type": "boolean"},
            },
        },
    },
}

TIER3_RUBRIC_SCHEMA = {
    "type": "object",
    "required": ["dimensions", "overall", "feedback"],
    "additionalProperties": False,
    "properties": {
        "dimensions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "score", "nearest_exemplar",
                             "rationale"],
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "nearest_exemplar": {"enum": ["strong", "adequate",
                                                  "weak"]},
                    "rationale": {"type": "string"},
                },
            },
        },
        "overall": {"type": "number", "minimum": 0, "maximum": 1},
        "feedback": {"type": "string"},
    },
}

EXPLAIN_BACK_SCHEMA = {
    "type": "object",
    "required": ["probes", "grade_cap"],
    "additionalProperties": False,
    "properties": {
        "probes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["concept", "verdict", "evidence",
                             "followup_question"],
                "additionalProperties": False,
                "properties": {
                    "concept": {"type": "string"},
                    "verdict": {"enum": ["understood", "surface",
                                         "misconception"]},
                    "evidence": {"type": "string"},
                    "followup_question": {"type": "string"},
                },
            },
        },
        "grade_cap": {"anyOf": [{"type": "null"},
                                {"type": "number",
                                 "minimum": 0, "maximum": 1}]},
    },
}

STAGE_OUTPUT_SCHEMAS = {
    "intake": INTAKE_SCHEMA,
    "harvest": HARVEST_SCHEMA,
    "sequence": SEQUENCE_SCHEMA,
    "audit": CLAIM_AUDIT_SCHEMA,
    "tier3": TIER3_RUBRIC_SCHEMA,
    "explain": EXPLAIN_BACK_SCHEMA,
}


def stage_output_schema(stage: str) -> dict | None:
    """JSON Schema for a stage name or family; None for raw-markdown
    stages ('lesson_00-x' resolves through its family, like versions)."""
    return STAGE_OUTPUT_SCHEMAS.get(stage.split("_", 1)[0])
