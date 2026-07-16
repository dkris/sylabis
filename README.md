# sylabis — the learning agent

sylabis turns learning into one continuous journey. Tell it what you want
to learn: it compiles a course from primary sources, teaches milestone by
milestone, grades the real artifacts you build, adapts the path when you
struggle or excel, and connects everything you verify into one growing
knowledge map. Concepts proven in one course become assumed knowledge in
the next — curricula connect instead of restarting from zero.

The learner's whole job is to learn. The agent does the rest.

## Install

```
curl -fsSL https://raw.githubusercontent.com/dkris/sylabis/main/install.sh | sh
```

That gives you `sy`, the agent, on your PATH (an isolated install under
`~/.local/share/sylabis` — no system Python touched; Python 3.10+ must be
installed). Re-run the same line to upgrade; add `-s -- --uninstall` to
remove. `sylabis` is installed too as the long-form alias.

```
export ANTHROPIC_API_KEY=sk-ant-...    # or put it in .env
sy                                     # talk mode — the agent drives the whole loop
```

Developing on sylabis itself? `pip install -e .` in a virtualenv gives
you the same `sy` command from your checkout.

## The surface

```
sy                      # talk — the agent drives the whole loop
sy web                  # the same journey in your browser
sy learn "topic"        # start a course in your journey
sy next                 # what to do now, across every course
sy submit               # grade the work sitting in your journey
sy journey              # progress + the knowledge map
sy attach SOURCE        # connect a course from another repo or path
```

No paths, no milestone ids, no flags required. Your journey lives in
`~/sylabis` (override with `$SYLABIS_HOME` or `--home`): courses under
`courses/`, the cross-course knowledge map at `knowledge.md`. `submit`
finds the milestone whose work is on disk and grades it.

Every learner is different, so the same journey has three doors with
identical powers: the terminal agent, the browser, and any MCP client.
Use whichever feels like home. The browser is the sylabis **Reading
Room** (design concept 1a): a calm single-column editorial surface —
warm paper, Didot over Georgia with mono labels — where you start a
course from the page itself, read lessons, submit work, and get the
grade told tier by tier; Sy waits behind an **Ask Sy** tab and slides in
as a right dock only when called. Plain HTML, no JS framework, no build
step; the one webfont degrades to Georgia offline.

The terminal is a real agent CLI, not a readline loop: replies stream in
as they generate, every tool call renders as a trace line with a result
preview (`⏺ submit_work(course: "…", …)` / `⎿ Grade: 92% — PASSED`), a
spinner covers the thinking, Ctrl-C abandons a turn without losing the
session, and slash commands (`/journey`, `/next`, `/clear`, `/help`)
answer the mechanical questions locally with no model round-trip.
Everything degrades to plain text when piped or when `NO_COLOR` is set.

The loop, if you prefer the verbs to the conversation:

```
sy learn "distill a small coding model for my MacBook M4" --hardware "M4 48GB"
sy next                      # read the lesson it points at
# do the work, drop artifact.md + reflection.md in the milestone dir
sy submit --hours 2.5        # grades, unlocks sidequests, injects remedials
```

## Connected curriculum

Every passing grade verifies the milestone's core concepts. The journey
accumulates them — with evidence: which course, which artifact, what
grade — and feeds them into every new compile as assumed knowledge, so
course N+1 builds on what course N proved instead of re-teaching it.
`sy journey` renders the map; concepts verified in more than one
course show up as connections, and `sy web` draws the whole thing
as a graph: courses on one side, verified concepts on the other, bridge
concepts ringed where courses meet.

Courses don't have to live in the journey to join it. `sy attach`
connects content from anywhere — a git URL clones the bundle in, a local
path symlinks it — and its verified knowledge counts like any other's,
so curricula connect across repositories, not just within one directory.

## Claude as interface (MCP)

```
python -m sylabis.cli serve                  # the whole journey
python -m sylabis.cli serve ~/path/course    # one bundle (legacy scope)
```

Journey scope serves the same nine tools the interactive agent uses —
`journey`, `start_course`, `get_lesson`, `submit_work`, `knowledge_map`,
… — so Claude Desktop can run the entire loop across every course:

```json
{"mcpServers": {"sylabis": {
  "command": "python", "args": ["-m", "sylabis.cli", "serve"]}}}
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
sylabis/
├── agent.py        the harness: one streaming tool-use loop; sylabis IS
│                   this agent (terminal live, web silent — same loop)
├── console.py      the terminal experience: streamed prose, ⏺ tool-call
│                   trace lines with ⎿ result previews, spinner, ANSI-safe
├── tools.py        the agent surface — one journey-scoped tool registry
│                   shared by the terminal agent, the web app, and MCP
├── web.py          the Reading Room: stdlib web app in the sylabis design
│                   system — compile, lessons, tiered grades, Sy dock, map
├── journey.py      connected curriculum: courses (attached from any repo),
│                   verified knowledge, next-step, the knowledge map
├── llm.py          one entry point for all model calls; mock mode
├── prompts.py      the pipeline stages ARE these prompts — version them like code
├── compiler.py     intake → harvest → sequence → emit → self-test (+ remedials)
├── verify.py       harvest locator verification (batched arXiv API, doi.org, HEAD)
├── okf.py          ALL markdown+frontmatter production; okf.yaml bundle manifest
├── grader.py       T1 structural → T2 claim audit → T3 rubric → explain-back caps
├── path_engine.py  decide(): legible rules table · actuate(): unlocks + remedials
├── events.py       append-only JSONL — the pathway graph seed
├── mcp_server.py   MCP stdio server: journey scope or per-course scope
└── cli.py          talk | web | learn | next | submit | journey | attach | serve
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
- The web app is local and single-learner: no auth, bind to 127.0.0.1 only.
- Attached courses are clones/links; nothing pulls them automatically —
  `git pull` in the course directory refreshes one.
- Single-user, local only. That is the point of a prototype.
