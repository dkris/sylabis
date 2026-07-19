"""
MCP stdio server. Hand-rolled JSON-RPC 2.0 (no SDK): newline-delimited
UTF-8, one message per line, no Content-Length framing. The invariant that
keeps the transport alive is that stdout carries protocol messages and
NOTHING else — every log line and every stray print from called code (the
compiler narrates to stdout) is redirected to stderr.

Two scopes, one transport:
  serve <course_dir>  — the original per-course tools over one bundle
  serve               — the journey tools (tools.py), same registry the
                        interactive agent uses, across every course
"""
import contextlib
import json
import re
import sys
from pathlib import Path

import yaml

from . import __version__
from . import events
from . import journey
from .llm import LLM
from .tools import JourneyTools, ToolError

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_PROTOCOL = "2025-11-25"
# Protocol versions are YYYY-MM-DD date strings per the MCP spec.
_PROTOCOL_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")

# Surfaced as an isError:true result so the model can self-correct rather
# than a JSON-RPC error that aborts the call.
_ToolError = ToolError


class _UnknownTool(Exception):
    """Tool name is not one we expose — a client mistake, not a tool failure."""


class _InvalidParams(Exception):
    """Request params are malformed (JSON-RPC -32602)."""


class _MethodNotFound(Exception):
    """Unknown JSON-RPC method (clients probe resources/list, prompts/list)."""


class MCPServer:
    def __init__(self, course_dir: Path | None = None, mock: bool = False,
                 home_dir: Path | None = None):
        self.course_dir = Path(course_dir) if course_dir else None
        self.mock = mock
        self._llm_instance = None  # built lazily; read tools need no model
        # name -> (handler, description, inputSchema), insertion order = list order
        if self.course_dir is None:
            self._tools = JourneyTools(journey.home(home_dir), mock=mock).registry
        else:
            self._tools = self._build_tools()

    # ---------------------------------------------------------------- runtime

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            response = self._handle_line(line)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

    def _handle_line(self, line: str):
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            return self._error(None, -32700, f"Parse error: {e}")

        # Notifications carry no id and never get a reply.
        if not isinstance(msg, dict) or "id" not in msg:
            try:
                self._handle_notification(msg)
            except Exception as e:  # a bad notification must not kill the loop
                self._log(f"notification error: {e}")
            return None

        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        try:
            result = self._dispatch(method, params)
        except _MethodNotFound:
            return self._error(mid, -32601, f"Method not found: {method}")
        except (_UnknownTool, _InvalidParams) as e:
            return self._error(mid, -32602, str(e))
        except Exception as e:
            return self._error(mid, -32603, f"Internal error: {e}")
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def _handle_notification(self, msg: dict) -> None:
        # notifications/initialized and friends are consumed silently.
        self._log(f"notification: {msg.get('method')}")

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return self._initialize(params)
        if method == "tools/list":
            return {"tools": [{"name": n, "description": d, "inputSchema": s}
                              for n, (_, d, s) in self._tools.items()]}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "ping":
            return {}
        raise _MethodNotFound()

    def _initialize(self, params: dict) -> dict:
        # Version negotiation, narrowed (WS2): a version we support is
        # echoed per-spec; a well-formed version we do NOT support gets
        # our latest (also per-spec — the client decides whether to
        # disconnect) but is flagged loudly on stderr instead of being
        # answered silently; anything malformed or missing is rejected.
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not _PROTOCOL_RE.match(requested):
            raise _InvalidParams(
                f"Invalid or missing protocolVersion: {requested!r} "
                f"(expected a YYYY-MM-DD version string)")
        if requested in PROTOCOL_VERSIONS:
            version = requested
        else:
            self._log(f"client requested unsupported protocolVersion "
                      f"{requested!r}; offering {LATEST_PROTOCOL} "
                      f"(supported: {', '.join(PROTOCOL_VERSIONS)})")
            version = LATEST_PROTOCOL
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "sylabis", "version": __version__},
        }

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        args = params.get("arguments") or {}
        entry = self._tools.get(name)
        if entry is None:
            raise _UnknownTool(f"Unknown tool: {name}")
        handler = entry[0]
        # Redirect stdout to stderr for the whole call: the compiler and any
        # called code print progress, which would otherwise corrupt the stream.
        try:
            with contextlib.redirect_stdout(sys.stderr):
                text = handler(args)
            return {"content": [{"type": "text", "text": text}], "isError": False}
        except _ToolError as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # a tool bug is self-correctable, not fatal
            self._log(f"tool {name} failed: {e}")
            return {"content": [{"type": "text", "text": f"Tool error: {e}"}],
                    "isError": True}

    def _error(self, mid, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}

    def _log(self, msg: str) -> None:
        print(f"[sylabis.mcp] {msg}", file=sys.stderr, flush=True)

    def _llm(self) -> LLM:
        if self._llm_instance is None:
            self._llm_instance = LLM(mock=self.mock)
        return self._llm_instance

    # ------------------------------------------------------------ file access

    _ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*$")

    def _safe_id(self, value: str, what: str) -> str:
        """Tool args are model-controlled input: ids become path segments,
        so anything with separators or leading dots is refused outright."""
        if not isinstance(value, str) or not self._ID.match(value) \
                or ".." in value:
            raise _ToolError(f"Invalid {what}: {value!r}")
        return value

    def _read(self, rel: str, missing_msg: str) -> str:
        path = (self.course_dir / rel).resolve()
        if not path.is_relative_to(self.course_dir.resolve()):
            raise _ToolError(f"Path {rel!r} escapes the course bundle.")
        if not path.exists():
            raise _ToolError(missing_msg)
        return path.read_text()

    def _manifest(self) -> dict:
        return yaml.safe_load((self.course_dir / "course.yaml").read_text())

    def _passed(self, milestone_id: str) -> bool:
        gpath = self.course_dir / milestone_id / "grade.yaml"
        if not gpath.exists():
            return False
        return bool((yaml.safe_load(gpath.read_text()) or {}).get("passed"))

    # ------------------------------------------------------------- read tools

    def _t_course_overview(self, args: dict) -> str:
        return self._read("index.md", "Course index.md not found in bundle.")

    def _t_get_lesson(self, args: dict) -> str:
        mid = self._safe_id(args["milestone_id"], "milestone_id")
        text = self._read(f"{mid}/LESSON.md",
                          f"No lesson for milestone {mid!r}.")
        # Log the start once — not on re-reads after the milestone is graded.
        if not (self.course_dir / mid / "grade.yaml").exists():
            events.emit(self.course_dir, "milestone.started", {"milestone_id": mid})
        return text

    def _t_list_sources(self, args: dict) -> str:
        return self._read("knowledge/index.md", "No knowledge base in bundle.")

    def _t_get_source(self, args: dict) -> str:
        sid = self._safe_id(args["source_id"], "source_id")
        return self._read(f"knowledge/source-{sid}.md",
                          f"No source {sid!r} in the knowledge base.")

    def _t_get_portfolio(self, args: dict) -> str:
        return self._read("portfolio/index.md", "No portfolio in bundle.")

    def _t_get_claim(self, args: dict) -> str:
        mid = self._safe_id(args["milestone_id"], "milestone_id")
        return self._read(
            f"portfolio/claims/{mid}.md",
            f"No verified claim for {mid!r} yet — pass its milestone first.")

    def _t_next_milestone(self, args: dict) -> str:
        manifest = self._manifest()
        for m in manifest["milestones"]:
            if self._passed(m["id"]):
                continue
            deps = m.get("depends_on", [])
            if all(self._passed(d) for d in deps):
                return (f"Next: {m['id']} — {m['title']} "
                        f"(~{m['estimated_hours']}h)\nRead: {m['id']}/LESSON.md")
            return f"Blocked: {m['id']} waiting on {deps}"
        return "Course complete. Check portfolio/index.md."

    def _t_grade_status(self, args: dict) -> str:
        manifest = self._manifest()
        lines = []
        for m in manifest["milestones"]:
            gpath = self.course_dir / m["id"] / "grade.yaml"
            if not gpath.exists():
                status = "not started"
            else:
                g = yaml.safe_load(gpath.read_text()) or {}
                verdict = "passed" if g.get("passed") else "not yet"
                status = (f"{verdict} — grade {g.get('grade', 0):.0%}, "
                          f"attempt {g.get('attempt', 1)}")
            lines.append(f"{m['id']}: {status}")
        return "\n".join(lines)

    # ------------------------------------------------------------ write tools

    def _t_submit_artifact(self, args: dict) -> str:
        mid = self._safe_id(args["milestone_id"], "milestone_id")
        m_dir = self.course_dir / mid
        if not (m_dir / "checkpoint.yaml").exists():
            raise _ToolError(f"Unknown milestone {mid!r} — no checkpoint found.")
        m_dir.mkdir(parents=True, exist_ok=True)
        (m_dir / "artifact.md").write_text(args["artifact"])
        (m_dir / "reflection.md").write_text(args["reflection"])

        from .grader import grade
        from .path_engine import decide, actuate

        llm = self._llm()
        result = grade(self.course_dir, mid, llm,
                       hours_actual=args.get("hours_actual"))
        milestone_entry = next(
            (m for m in self._manifest()["milestones"] if m["id"] == mid), None)
        if milestone_entry is None:
            raise _ToolError(f"{mid!r} graded but absent from course.yaml.")
        decisions = decide(self.course_dir, milestone_entry, result)
        actions = actuate(self.course_dir, decisions, llm=llm)

        lines = [result["feedback"], "", "Path decisions:"]
        if not decisions:
            lines.append("  (none)")
        for d in decisions:
            lines.append(f"  - {d['action']} -> {d.get('target', '')}")
        for a in actions:
            lines.append(f"  {a}")
        return "\n".join(lines)

    def _t_compile_course(self, args: dict) -> str:
        from .compiler import compile_course

        profile = {"weekly_hours": args.get("hours", 5),
                   "hardware": args.get("hardware", ""),
                   "prior_knowledge": []}
        out_dir = Path(args["out_dir"])
        try:
            compile_course(args["topic"], profile, out_dir, self._llm())
        except SystemExit as e:  # declined topic or self-test failure
            raise _ToolError(str(e))
        return (out_dir / "index.md").read_text()

    # --------------------------------------------------------------- registry

    def _build_tools(self) -> dict:
        def obj(props, required):
            return {"type": "object", "properties": props,
                    "required": required, "additionalProperties": False}

        s_mid = {"milestone_id": {"type": "string",
                                  "description": "Milestone id, e.g. '00-data-audit'."}}
        return {
            "course_overview": (
                self._t_course_overview,
                "Read the course index (title, milestones, links). Call first "
                "to orient yourself in the course.",
                obj({}, [])),
            "get_lesson": (
                self._t_get_lesson,
                "Read a milestone's LESSON.md; call before working on it. "
                "Marks the milestone as started.",
                obj(s_mid, ["milestone_id"])),
            "list_sources": (
                self._t_list_sources,
                "List the primary sources the course compiles from. Call to "
                "find which source backs a milestone.",
                obj({}, [])),
            "get_source": (
                self._t_get_source,
                "Read one primary source's page by id. Call after list_sources "
                "to get its locator and what to take from it.",
                obj({"source_id": {"type": "string",
                                   "description": "Source id from list_sources."}},
                    ["source_id"])),
            "get_portfolio": (
                self._t_get_portfolio,
                "Read the portfolio index of verified competency claims earned "
                "so far.",
                obj({}, [])),
            "get_claim": (
                self._t_get_claim,
                "Read the verified competency claim for a passed milestone; "
                "errors if the milestone has no claim yet.",
                obj(s_mid, ["milestone_id"])),
            "next_milestone": (
                self._t_next_milestone,
                "Report the next milestone to work on given what is passed and "
                "which dependencies are met.",
                obj({}, [])),
            "grade_status": (
                self._t_grade_status,
                "Summarize every milestone's grade status (passed/attempts or "
                "not started) in one line each.",
                obj({}, [])),
            "submit_artifact": (
                self._t_submit_artifact,
                "Submit your artifact and reflection for a milestone; grades it "
                "and returns feedback and path decisions.",
                obj({**s_mid,
                     "artifact": {"type": "string",
                                  "description": "The artifact markdown to grade."},
                     "reflection": {"type": "string",
                                    "description": "Your reflection on the work."},
                     "hours_actual": {"type": ["number", "null"],
                                      "description": "Hours you actually spent, "
                                                     "or null."}},
                    ["milestone_id", "artifact", "reflection"])),
            "compile_course": (
                self._t_compile_course,
                "Compile a NEW course bundle for a topic into out_dir; returns "
                "the new course's index. Takes up to a minute.",
                obj({"topic": {"type": "string",
                               "description": "What the learner wants to learn."},
                     "out_dir": {"type": "string",
                                 "description": "Directory to write the bundle to."},
                     "hours": {"type": "integer",
                               "description": "Weekly hours available (default 5)."},
                     "hardware": {"type": "string",
                                  "description": "Hardware constraints, if any."}},
                    ["topic", "out_dir"])),
        }
