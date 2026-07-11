# coursec — the learning agent

coursec turns learning into one continuous journey. Tell it what you want
to learn: it compiles a course from primary sources, teaches milestone by
milestone, grades the real artifacts you build, adapts the path when you
struggle or excel, and connects everything you verify into one growing
knowledge map. Concepts proven in one course become assumed knowledge in
the next — curricula connect instead of restarting from zero.

The learner's whole job is to learn. The agent does the rest.

## Install

```
pip install -r requirements.txt        # or: pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...    # or put it in .env
```

## The surface

```
coursec                      # talk — the agent drives the whole loop
coursec learn "topic"        # start a course in your journey
coursec next                 # what to do now, across every course
coursec submit               # grade the work sitting in your journey
coursec journey              # progress + the knowledge map
```

No paths, no milestone ids, no flags required. Your journey lives in
`~/coursec` (override with `$COURSEC_HOME` or `--home`): courses under
`courses/`, the cross-course knowledge map at `knowledge.md`. `submit`
finds the milestone whose work is on disk and grades it.

The loop, if you prefer the verbs to the conversation:

```
coursec learn "distill a small coding model for my MacBook M4" --hardware "M4 48GB"
coursec next                 # read the lesson it points at
# do the work, drop artifact.md + reflection.md in the milestone dir
coursec submit --hours 2.5   # grades, unlocks sidequests, injects remedials
```

## Connected curriculum

Every passing grade verifies the milestone's core concepts. The journey
accumulates them — with evidence: which course, which artifact, what
grade — and feeds them into every new compile as assumed knowledge, so
course N+1 builds on what course N proved instead of re-teaching it.
`coursec journey` renders the map; concepts verified in more than one
course show up as connections.

## Claude as interface (MCP)

```
python -m coursec.cli serve                  # the whole journey
python -m coursec.cli serve ~/path/course    # one bundle (legacy scope)
```

Journey scope serves the same nine tools the interactive agent uses —
`journey`, `start_course`, `get_lesson`, `submit_work`, `knowledge_map`,
… — so Claude Desktop can run the entire loop across every course:

```json
{"mcpServers": {"coursec": {
  "command": "python", "args": ["-m", "coursec.cli", "serve"]}}}
```

## GitHub as interface

Every compiled bundle ships `.github/workflows/grade.yml`: push the
bundle to a repo, add the `ANTHROPIC_API_KEY` secret, and pushing
`artifact.md`/`reflection.md` triggers grading — feedback lands as a
commit (push) or PR comment (pull request).

## Mock mode

`learn`, `submit`, `compile`, and `grade` take `--mock`, which serves
fixture responses from `fixtures/` instead of calling the API —
deterministic tests against a stochastic pipeline. The adversarial suite
lives in `tests/` (`python -m tests.run_all`).

## Architecture

```
coursec/
├── agent.py        the harness: a thin tool-use loop; coursec IS this agent
├── tools.py        the agent surface — one journey-scoped tool registry
│                   shared by the interactive agent and the MCP server
├── journey.py      connected curriculum: courses, verified knowledge,
│                   next-step and the knowledge map — all read from disk
├── llm.py          one entry point for all model calls; mock mode
├── prompts.py      the pipeline stages ARE these prompts — version them like code
├── compiler.py     intake → harvest → sequence → emit → self-test (+ remedials)
├── verify.py       harvest locator verification (batched arXiv API, doi.org, HEAD)
├── okf.py          ALL markdown+frontmatter production; okf.yaml bundle manifest
├── grader.py       T1 structural → T2 claim audit → T3 rubric → explain-back caps
├── path_engine.py  decide(): legible rules table · actuate(): unlocks + remedials
├── events.py       append-only JSONL — the pathway graph seed
├── mcp_server.py   MCP stdio server: journey scope or per-course scope
└── cli.py          talk | learn | next | submit | journey | serve (+ plumbing)
```

Design decisions that are deliberate, not shortcuts:
- A journey is a directory of courses; a course is a directory; state is
  YAML. The journey owns no database and no copies — it reads the bundles,
  so it can never disagree with them.
- The agent harness is thin on purpose: every capability is a tool, so
  the terminal agent and any MCP client get exactly the same powers.
- The guide never writes the learner's artifact — the work must be the
  learner's own, or the grades (and the portfolio built on them) mean nothing.
- The self-test gate (structural + OKF conformance) blocks shipping a broken course.
- The claim audit blocks Tier 3 judgment on artifacts that overclaim.
- Explain-back always runs and can cap the grade — rubric gaming defense.
- Tier 3 exemplar scoring stays OFF until a human seeds the exemplar set;
  an uncalibrated judge is worse than the claim-audit fallback.
- Dead-source checks flag and continue — verification never blocks a compile.
- events.jsonl is written from day one so the pathway graph exists in three years.

## Known gaps (honest list)

- Tier 3 exemplar sets are unseeded (4–6 h human task per course before enabling).
- `starter/run_benchmark.py` is a scaffold the learner fills in; the grader
  runs it but ships no reference implementation per milestone yet.
- The knowledge map connects courses by verified concepts; it does not yet
  suggest what to learn next from the graph (collect first, infer later).
- Web renderer for bundles not built (P2).
- Single-user, local only. That is the point of a prototype.
