"""
The agent harness. `sylabis` with no arguments lands here: a conversation
in the terminal where the model drives the whole learn → submit → grade →
adapt loop through the journey tools. The harness stays thin — everything
that matters lives in the tools (tools.py) and the guide prompt
(prompts.py) — and the loop itself renders nothing: it emits typed events
on a bus (bus.py) and whoever subscribed paints them. The terminal
subscribes a ConsoleRenderer; the web harness subscribes nothing and
takes the returned text; a future streaming endpoint is one more
subscriber, not a fork of this loop.

The conversation survives the process: messages persist to the journey's
session.jsonl (session.py) after each completed turn, so closing the
terminal mid-course costs nothing. Ctrl-C abandons the current turn whole
— memory and file always agree.
"""
from pathlib import Path

from . import bus
from . import journey
from .console import Console, ConsoleRenderer
from .llm import LLM
from .prompts import GUIDE_SYSTEM
from .session import Session
from .tools import JourneyTools, ToolError

MAX_TOOL_ROUNDS = 12  # per user turn; a guide that needs more is looping

_HELP = """\
/help      these commands
/journey   where you are — courses, progress, knowledge (no model call)
/next      the single next action (no model call)
/clear     start a fresh conversation (your journey is untouched)
/quit      leave (Ctrl-D works too)
Ctrl-C     interrupt the current turn; nothing from it is kept"""


class Agent:
    def __init__(self, home_dir: Path, mock: bool = False,
                 llm: LLM | None = None, console: Console | None = None):
        self.home = Path(home_dir)
        self.tools = JourneyTools(self.home, mock=mock)
        self.llm = llm or LLM(mock=mock)
        self.ui = console if console is not None else Console()
        self.bus = bus.Bus()
        self.bus.subscribe(ConsoleRenderer(self.ui))
        self.session = Session(self.home)
        self.messages: list[dict] = self.session.load()
        self.specs = [{"name": n, "description": d, "input_schema": s}
                      for n, (_, d, s) in self.tools.registry.items()]

    # ---------------------------------------------------------------- REPL

    def run(self) -> None:
        status = self._status_lines()
        if self.messages:
            status.append(f"resumed conversation ({len(self.messages)} "
                          "messages) · /clear starts fresh")
        self.ui.header("sylabis — the learning agent",
                       status + ["/help for commands · Ctrl-D to leave"])
        while True:
            try:
                user = input(self._prompt()).strip()
            except EOFError:
                self.ui.turn_end()
                return
            except KeyboardInterrupt:
                print()
                self.ui.notice("(Ctrl-D or /quit to leave)")
                continue
            if not user:
                continue
            if user.startswith("/"):
                if self._command(user):
                    return
                continue
            self.converse(user)
            self.ui.turn_end()

    def _prompt(self) -> str:
        return self.ui.cyan("\n❯ ") if self.ui.color else "\n❯ "

    def _status_lines(self) -> list[str]:
        steps = journey.next_steps(self.home)
        if not steps:
            return ["your journey is empty — say what you want to learn"]
        know = len(journey.knowledge(self.home))
        line = (f"{len(steps)} course{'s' if len(steps) != 1 else ''} · "
                f"{know} verified concept{'s' if know != 1 else ''}")
        ready = [s for s in steps if s["status"] == "ready"]
        if ready:
            s = ready[0]
            line += f" · next: {s['milestone_id']} — {s['title']}"
        return [line]

    def _command(self, cmd: str) -> bool:
        """Local slash commands — mechanical questions get instant answers
        from the tools, no model round-trip. Returns True to exit."""
        name = cmd.split()[0].lower()
        if name in ("/quit", "/exit"):
            return True
        if name == "/help":
            self.ui.text_block("\n" + _HELP)
        elif name == "/clear":
            self.messages.clear()
            self.session.clear()
            self.ui.notice("conversation cleared — the journey is untouched")
        elif name in ("/journey", "/next"):
            tool = "journey" if name == "/journey" else "next_step"
            self.ui.tool_call(tool, {})
            try:
                self.ui.tool_result(self.tools.call(tool, {}))
            except ToolError as e:
                self.ui.tool_result(str(e), error=True)
        else:
            self.ui.notice(f"unknown command {name} — /help lists them")
        return False

    # ---------------------------------------------------------------- turns

    def converse(self, text: str) -> str:
        """One exchange against the agent's own transcript: append the
        user message, run the turn, persist what survived. An interrupted
        turn leaves no trace — not in memory, not on disk. Every harness
        (terminal REPL, web chat) goes through here so persistence can
        never diverge between doors."""
        if not self.messages:
            # First turn carries the standing instruction to orient.
            text = f"(new session — orient first)\n{text}"
        prev = len(self.messages)
        self.messages.append({"role": "user", "content": text})
        reply = self.turn(self.messages)
        if len(self.messages) == prev + 1:  # rolled back: drop the user msg
            del self.messages[prev:]
            return reply
        self.session.append(self.messages[prev:])
        return reply

    def turn(self, messages: list[dict]) -> str:
        """One user turn: stream the model, run any tools it asks for, feed
        results back, repeat until it answers in text. Emits bus events as
        it goes and returns the full reply. Ctrl-C rolls the whole turn
        back — the transcript never holds a dangling tool call."""
        base = len(messages)
        said: list[str] = []
        self.bus.emit(bus.TurnStarted())
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                self.bus.emit(bus.ModelCallStarted())
                streamed = False

                def on_text(delta: str) -> None:
                    nonlocal streamed
                    streamed = True
                    self.bus.emit(bus.TextDelta(delta))

                resp = self.llm.chat(GUIDE_SYSTEM, messages, self.specs,
                                     on_text=on_text)
                content = []
                for b in resp.content:
                    d = b.model_dump()
                    for extra in getattr(b, "__api_exclude__", ()):
                        d.pop(extra, None)
                    content.append(d)
                messages.append({"role": "assistant", "content": content})
                texts = [b.text.strip() for b in resp.content
                         if b.type == "text" and b.text.strip()]
                said += texts
                if not streamed:
                    for t in texts:
                        self.bus.emit(bus.AssistantText(t))
                elif texts:
                    self.bus.emit(bus.TextDelta("\n"))
                if resp.stop_reason != "tool_use":
                    self.bus.emit(bus.TurnEnded())
                    return "\n\n".join(said)
                results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    self.bus.emit(bus.ToolCallStarted(block.name,
                                                      block.input))
                    result = self._run_tool(block)
                    self.bus.emit(bus.ToolResult(block.name,
                                                 result["content"],
                                                 result["is_error"]))
                    results.append(result)
                messages.append({"role": "user", "content": results})
            reason = "stopped — too many tool rounds in one turn"
            self.bus.emit(bus.TurnStopped(reason))
            return "\n\n".join(said + [f"({reason})"])
        except KeyboardInterrupt:
            del messages[base:]
            self.bus.emit(bus.TurnInterrupted())
            return ""

    def _run_tool(self, block) -> dict:
        try:
            text, is_error = self.tools.call(block.name, block.input), False
        except ToolError as e:
            text, is_error = str(e), True
        except KeyboardInterrupt:
            raise
        except Exception as e:  # a tool bug is self-correctable, not fatal
            text, is_error = f"Tool error: {e}", True
        return {"type": "tool_result", "tool_use_id": block.id,
                "content": text, "is_error": is_error}
