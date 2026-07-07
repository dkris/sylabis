# coursec — course compiler prototype

The core loop of the learning-compiler business, runnable today:
compile a course from a topic prompt, grade real artifacts, adapt the
path, log every event for the future pathway graph. Everything on disk
is an OKF document — the course IS the credential.

## Install

```
pip install -r requirements.txt        # or: pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...    # or put it in .env
```

## The loop

```
# 1. Compile a course
python -m coursec.cli compile "distill a small coding model for my MacBook M4" \
    --out ~/courses/distill-m4 --hours 6 --hardware "M4 48GB"

# 2. See what to do
python -m coursec.cli next ~/courses/distill-m4

# 3. Do the milestone, drop artifact.md + reflection.md in the milestone dir

# 4. Grade it (unlocks sidequests, injects remedials when needed)
python -m coursec.cli grade ~/courses/distill-m4 00-<milestone> --hours-actual 2.5

# 5. Repeat. Verified claims accrue in portfolio/; events in events.jsonl.
```

## Claude as interface (MCP)

```
python -m coursec.cli serve ~/courses/distill-m4
```

Point Claude Desktop at that command and the whole loop runs inside a
conversation: ten tools including `submit_artifact` (write + grade +
path decisions in one call) and `compile_course`.

```json
{"mcpServers": {"coursec": {
  "command": "python",
  "args": ["-m", "coursec.cli", "serve", "/absolute/path/to/course"]}}}
```

## GitHub as interface

Every compiled bundle ships `.github/workflows/grade.yml`: push the
bundle to a repo, add the `ANTHROPIC_API_KEY` secret, and pushing
`artifact.md`/`reflection.md` triggers grading — feedback lands as a
commit (push) or PR comment (pull request).

## Mock mode

Every command takes `--mock`, which serves fixture responses from
`fixtures/` instead of calling the API — deterministic tests against a
stochastic pipeline. The adversarial grader suite lives in `tests/`
(`python -m tests.run_all`).

## Architecture

```
coursec/
├── llm.py          one entry point for all model calls; mock mode
├── prompts.py      the pipeline stages ARE these prompts — version them like code
├── compiler.py     intake → harvest → sequence → emit → self-test (+ remedial mini-compiles)
├── verify.py       harvest locator verification (batched arXiv API, doi.org, HEAD)
├── okf.py          ALL markdown+frontmatter production; okf.yaml bundle manifest
├── grader.py       T1 structural → T2 claim audit → T3 rubric (exemplar LLM or
│                   executable scripts) → explain-back always caps
├── path_engine.py  decide(): legible rules table · actuate(): unlocks + remedials
├── events.py       append-only JSONL — the pathway graph seed
├── mcp_server.py   MCP stdio server: 8 read tools + submit_artifact + compile_course
└── cli.py          compile | grade | next | serve
```

Design decisions that are deliberate, not shortcuts:
- A course is a directory; state is YAML. No DB until the MVP needs multi-user.
- The self-test gate (structural + OKF conformance) blocks shipping a broken course.
- The claim audit blocks Tier 3 judgment on artifacts that overclaim.
- Explain-back always runs and can cap the grade — rubric gaming defense.
- Tier 3 exemplar scoring stays OFF until a human seeds the exemplar set;
  an uncalibrated judge is worse than the claim-audit fallback.
- Dead-source checks flag and continue — verification never blocks a compile.
- events.jsonl is written from day one so the graph exists in three years.

## Known gaps (honest list)

- Tier 3 exemplar sets are unseeded (4–6 h human task per course before enabling).
- `starter/run_benchmark.py` is a scaffold the learner fills in; the grader
  runs it but ships no reference implementation per milestone yet.
- Web renderer for bundles not built (P2).
- Single-user, local only. That is the point of a prototype.
