# coursec — course compiler prototype

The core loop of the learning-compiler business, runnable today:
compile a course from a topic prompt, grade real artifacts, adapt the
path, log every event for the future pathway graph.

## Install

```
pip install anthropic pyyaml
export ANTHROPIC_API_KEY=sk-ant-...
```

## The loop

```
# 1. Compile a course
python -m coursec.cli compile "distill a small coding model for my MacBook M4" \
    --out ~/courses/distill-m4 --hours 6 --hardware "M4 48GB"

# 2. See what to do
python -m coursec.cli next ~/courses/distill-m4

# 3. Do the milestone, drop artifact.md + reflection.md in the milestone dir

# 4. Grade it
python -m coursec.cli grade ~/courses/distill-m4 00-<milestone> --hours-actual 2.5

# 5. Repeat. Portfolio accrues in portfolio/state.yaml.
#    Everything is logged to events.jsonl.
```

## Mock mode

Every command takes `--mock`, which serves fixture responses from
`fixtures/` instead of calling the API. This is how you write
deterministic tests against a stochastic pipeline, and how you run
the adversarial grader suite (Layer 2 of the validation plan) in CI.

## Architecture

```
coursec/
├── llm.py          one entry point for all model calls; mock mode
├── prompts.py      the pipeline stages ARE these prompts — version them like code
├── compiler.py     intake → harvest → sequence → emit → self-test
├── grader.py       Tier 1 deterministic, Tier 2 claim audit, explain-back always
├── path_engine.py  legible rules table, no black box
├── events.py       append-only JSONL — the pathway graph seed
└── cli.py          compile | grade | next
```

Design decisions that are deliberate, not shortcuts:
- A course is a directory; state is YAML. No DB until the MVP needs multi-user.
- The self-test gate blocks shipping a structurally broken course.
- The claim audit blocks Tier 3 judgment on artifacts that overclaim.
- Explain-back always runs and can cap the grade — rubric gaming defense.
- events.jsonl is written from day one so the graph exists in three years.

## Known prototype gaps (MVP work, listed honestly)

- Tier 3 exemplar-calibrated rubric scoring is stubbed (base_score).
- Technical rubric scripts are per-course; the compiler doesn't generate them yet.
- Harvest trusts the model's source list; MVP adds URL verification via web fetch.
- Remedial modules are signaled but not yet generated on the fly.
- Single-user, local only. That is the point of a prototype.
