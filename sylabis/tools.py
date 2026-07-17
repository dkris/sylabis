"""
Journey tools — the agent surface. One registry of learner-facing tools
over the whole journey, used unchanged by both harnesses: the interactive
agent (agent.py) and the MCP server (mcp_server.py serving a journey).
Every handler takes a JSON-args dict and returns text; failures raise
ToolError so the model can self-correct instead of the harness dying.
"""
import re
from pathlib import Path

import yaml

from . import events
from . import journey
from . import trust
from .llm import LLM


class ToolError(Exception):
    """A tool ran but could not complete (unknown course, grading error).
    Harnesses surface it as a recoverable tool result, never a crash."""


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_id(value, what: str) -> str:
    """Tool args are model-controlled and become path segments — anything
    with separators or leading dots is refused outright."""
    if not isinstance(value, str) or not _ID.match(value) or ".." in value:
        raise ToolError(f"Invalid {what}: {value!r}")
    return value


class JourneyTools:
    def __init__(self, home_dir: Path, mock: bool = False):
        self.home = Path(home_dir)
        self.mock = mock
        self._llm_instance = None  # built lazily; read tools need no model
        # name -> (handler, description, JSON schema); insertion order = list order
        self.registry = self._build()

    def call(self, name: str, args: dict) -> str:
        entry = self.registry.get(name)
        if entry is None:
            raise ToolError(f"Unknown tool: {name}")
        return entry[0](args or {})

    def _llm(self) -> LLM:
        if self._llm_instance is None:
            self._llm_instance = LLM(mock=self.mock)
        return self._llm_instance

    # ------------------------------------------------------------ file access

    def _course_dir(self, name: str) -> Path:
        cdir = journey.course_dir(self.home, _safe_id(name, "course"))
        if not (cdir / "course.yaml").exists():
            known = ", ".join(c.name for c in journey.course_dirs(self.home)) \
                    or "none yet — start_course begins one"
            raise ToolError(f"No course {name!r}. Courses: {known}")
        return cdir

    def _read(self, cdir: Path, rel: str, missing_msg: str) -> str:
        path = (cdir / rel).resolve()
        if not path.is_relative_to(cdir.resolve()):
            raise ToolError(f"Path {rel!r} escapes the course bundle.")
        if not path.exists():
            raise ToolError(missing_msg)
        return path.read_text()

    # ----------------------------------------------------------------- tools

    def _t_journey(self, args: dict) -> str:
        steps = journey.next_steps(self.home)
        if not steps:
            return ("The journey is empty. Ask the learner what they want to "
                    "learn, then start_course.")
        know = journey.knowledge(self.home)
        lines = [f"{len(steps)} courses, {len(know)} verified concepts.", ""]
        for s in steps:
            if s["status"] == "complete":
                lines.append(f"- {s['course']}: complete")
            elif s["status"] == "blocked":
                lines.append(f"- {s['course']}: blocked on "
                             f"{', '.join(s['blocked_on'])}")
            else:
                lines.append(f"- {s['course']}: next {s['milestone_id']} — "
                             f"{s['title']} (~{s['estimated_hours']}h)")
        return "\n".join(lines)

    def _t_start_course(self, args: dict) -> str:
        from .compiler import compile_course

        topic = args["topic"]
        profile = {"weekly_hours": args.get("hours") or 5,
                   "hardware": args.get("hardware", ""),
                   # the connection: new courses assume what is already proven
                   "prior_knowledge": journey.prior_knowledge(self.home)}
        out_dir = journey.new_course_dir(self.home, topic)
        try:
            compile_course(topic, profile, out_dir, self._llm())
        except SystemExit as e:  # declined topic or self-test failure
            raise ToolError(str(e))
        # You asked for this code to exist — your own compile is trusted.
        trust.grant(self.home, out_dir, "compiled")
        journey.emit_map(self.home)
        return (f"Course compiled into {out_dir.name!r}.\n\n"
                + (out_dir / "index.md").read_text())

    def _t_next_step(self, args: dict) -> str:
        steps = journey.next_steps(self.home)
        ready = [s for s in steps if s["status"] == "ready"]
        if not ready:
            if steps and all(s["status"] == "complete" for s in steps):
                return "Every course is complete. Time for a new topic?"
            if not steps:
                return "No courses yet — start_course begins the journey."
            return "\n".join(f"{s['course']}: {s['status']}" for s in steps)
        return "\n".join(
            f"{s['course']} → {s['milestone_id']} — {s['title']} "
            f"(~{s['estimated_hours']}h). Lesson: get_lesson."
            for s in ready)

    def _t_get_lesson(self, args: dict) -> str:
        cdir = self._course_dir(args["course"])
        mid = _safe_id(args["milestone_id"], "milestone_id")
        text = self._read(cdir, f"{mid}/LESSON.md",
                          f"No lesson for milestone {mid!r}.")
        # Log the start once — not on re-reads after the milestone is graded.
        if not (cdir / mid / "grade.yaml").exists():
            events.emit(cdir, "milestone.started", {"milestone_id": mid})
        return text

    def _t_submit_work(self, args: dict) -> str:
        from .grader import grade
        from .path_engine import decide, actuate

        cdir = self._course_dir(args["course"])
        mid = _safe_id(args["milestone_id"], "milestone_id")
        m_dir = cdir / mid
        if not (m_dir / "checkpoint.yaml").exists():
            raise ToolError(f"Unknown milestone {mid!r} — no checkpoint found.")
        m_dir.mkdir(parents=True, exist_ok=True)
        (m_dir / "artifact.md").write_text(args["artifact"])
        (m_dir / "reflection.md").write_text(args["reflection"])

        llm = self._llm()
        result = grade(cdir, mid, llm, hours_actual=args.get("hours_actual"),
                       run_scripts=trust.is_trusted(self.home, cdir))
        manifest = yaml.safe_load((cdir / "course.yaml").read_text())
        milestone = next((m for m in manifest["milestones"]
                          if m["id"] == mid), None)
        if milestone is None:
            raise ToolError(f"{mid!r} graded but absent from course.yaml.")
        decisions = decide(cdir, milestone, result)
        actions = actuate(cdir, decisions, llm=llm)
        journey.emit_map(self.home)

        lines = [result["feedback"], "", "Path decisions:"]
        if not decisions:
            lines.append("  (none)")
        for d in decisions:
            lines.append(f"  - {d['action']} -> {d.get('target', '')}")
        for a in actions:
            lines.append(f"  {a}")
        return "\n".join(lines)

    def _t_knowledge_map(self, args: dict) -> str:
        know = journey.knowledge(self.home)
        if not know:
            return "Nothing verified yet — pass a milestone to start the map."
        lines = []
        for e in know:
            refs = "; ".join(f"{ev['course']}/{ev['milestone_id']} "
                             f"({ev['grade']:.0%})" for ev in e["evidence"])
            lines.append(f"- {e['concept']} — {refs}")
        return "\n".join(lines)

    def _t_progress(self, args: dict) -> str:
        cdir = self._course_dir(args["course"])
        manifest = yaml.safe_load((cdir / "course.yaml").read_text())
        lines = []
        for m in manifest["milestones"]:
            gpath = cdir / m["id"] / "grade.yaml"
            if not gpath.exists():
                status = "not started"
            else:
                g = yaml.safe_load(gpath.read_text()) or {}
                verdict = "passed" if g.get("passed") else "not yet"
                status = (f"{verdict} — grade {g.get('grade', 0):.0%}, "
                          f"attempt {g.get('attempt', 1)}")
            lines.append(f"{m['id']}: {status}")
        return "\n".join(lines)

    def _t_list_sources(self, args: dict) -> str:
        cdir = self._course_dir(args["course"])
        return self._read(cdir, "knowledge/index.md",
                          "No knowledge base in this course.")

    def _t_get_source(self, args: dict) -> str:
        cdir = self._course_dir(args["course"])
        sid = _safe_id(args["source_id"], "source_id")
        return self._read(cdir, f"knowledge/source-{sid}.md",
                          f"No source {sid!r} in the knowledge base.")

    # --------------------------------------------------------------- registry

    def _build(self) -> dict:
        def obj(props, required):
            return {"type": "object", "properties": props,
                    "required": required, "additionalProperties": False}

        s_course = {"course": {"type": "string",
                               "description": "Course name from the journey "
                                              "overview."}}
        s_mid = {"milestone_id": {"type": "string",
                                  "description": "Milestone id, e.g. "
                                                 "'00-data-audit'."}}
        return {
            "journey": (
                self._t_journey,
                "Overview of the learner's journey: every course, its "
                "progress, and the verified-knowledge count. Call first to "
                "orient yourself.",
                obj({}, [])),
            "next_step": (
                self._t_next_step,
                "What the learner should do now, across all courses.",
                obj({}, [])),
            "start_course": (
                self._t_start_course,
                "Compile a new course for a topic into the journey. Verified "
                "knowledge from earlier courses is assumed automatically. "
                "Takes up to a minute.",
                obj({"topic": {"type": "string",
                               "description": "What the learner wants to learn."},
                     "hours": {"type": "integer",
                               "description": "Weekly hours available (default 5)."},
                     "hardware": {"type": "string",
                                  "description": "Hardware constraints, if any."}},
                    ["topic"])),
            "get_lesson": (
                self._t_get_lesson,
                "Read a milestone's lesson; call before the learner works on "
                "it. Marks the milestone as started.",
                obj({**s_course, **s_mid}, ["course", "milestone_id"])),
            "submit_work": (
                self._t_submit_work,
                "Submit the learner's artifact and reflection for grading; "
                "returns feedback and path decisions. The work must be the "
                "learner's own words.",
                obj({**s_course, **s_mid,
                     "artifact": {"type": "string",
                                  "description": "The learner's artifact markdown."},
                     "reflection": {"type": "string",
                                    "description": "The learner's reflection."},
                     "hours_actual": {"type": ["number", "null"],
                                      "description": "Hours actually spent, "
                                                     "or null."}},
                    ["course", "milestone_id", "artifact", "reflection"])),
            "knowledge_map": (
                self._t_knowledge_map,
                "Every concept the learner has verified, with its evidence "
                "trail across courses. Use it to connect new material to "
                "proven ground.",
                obj({}, [])),
            "progress": (
                self._t_progress,
                "Per-milestone grade status for one course.",
                obj(s_course, ["course"])),
            "list_sources": (
                self._t_list_sources,
                "List the primary sources one course compiles from.",
                obj(s_course, ["course"])),
            "get_source": (
                self._t_get_source,
                "Read one primary source's page by id, after list_sources.",
                obj({**s_course,
                     "source_id": {"type": "string",
                                   "description": "Source id from list_sources."}},
                    ["course", "source_id"])),
        }
