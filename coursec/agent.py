"""
The agent harness. `coursec` with no arguments lands here: a conversation
in the terminal where the model drives the whole learn → submit → grade →
adapt loop through the journey tools. The harness stays thin — everything
that matters lives in the tools (tools.py) and the guide prompt
(prompts.py) — but the *experience* is a modern agent CLI: prose streams
as it generates, every tool call renders as a trace line with a result
preview, a spinner covers the silences, slash commands handle the
mechanical stuff locally, and Ctrl-C abandons a turn without losing the
session. The same Agent runs silently under the web harness
(console disabled), so there is exactly one conversation loop.
"""
import sys
from pathlib import Path

from . import journey
from .console import Console
from .llm import LLM
from .prompts import GUIDE_SYSTEM
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
        self.specs = [{"name": n, "description": d, "input_schema": s}
                      for n, (_, d, s) in self.tools.registry.items()]

    # ---------------------------------------------------------------- REPL

    def run(self) -> None:
        self.ui.header("coursec — the learning agent", self._status_lines()
                       + ["/help for commands · Ctrl-D to leave"])
        messages: list[dict] = []
        first = True
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
                if self._command(user, messages):
                    return
                continue
            if first:
                # First turn carries the standing instruction to orient.
                user = f"(new session — orient first)\n{user}"
                first = False
            messages.append({"role": "user", "content": user})
            self.turn(messages)
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

    def _command(self, cmd: str, messages: list[dict]) -> bool:
        """Local slash commands — mechanical questions get instant answers
        from the tools, no model round-trip. Returns True to exit."""
        name = cmd.split()[0].lower()
        if name in ("/quit", "/exit"):
            return True
        if name == "/help":
            self.ui.text_block("\n" + _HELP)
        elif name == "/clear":
            messages.clear()
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

    def turn(self, messages: list[dict]) -> str:
        """One user turn: stream the model, run any tools it asks for, feed
        results back, repeat until it answers in text. Renders through the
        console as it goes and returns the full reply, so a silent harness
        (web chat) gets the same conversation without the terminal. Ctrl-C
        rolls the whole turn back — the transcript never holds a dangling
        tool call."""
        base = len(messages)
        said: list[str] = []
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                self.ui.turn_start()
                self.ui.spinner("thinking")
                streamed = False

                def on_text(delta: str) -> None:
                    nonlocal streamed
                    streamed = True
                    self.ui.text_delta(delta)

                resp = self.llm.chat(GUIDE_SYSTEM, messages, self.specs,
                                     on_text=on_text if self.ui.enabled
                                     else None)
                self.ui.stop_spinner()
                messages.append({"role": "assistant",
                                 "content": [b.model_dump()
                                             for b in resp.content]})
                texts = [b.text.strip() for b in resp.content
                         if b.type == "text" and b.text.strip()]
                said += texts
                if not streamed:
                    for t in texts:
                        self.ui.text_block(t)
                elif texts:
                    self.ui.text_delta("\n")
                if resp.stop_reason != "tool_use":
                    return "\n\n".join(said)
                results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    self.ui.tool_call(block.name, block.input)
                    self.ui.spinner(block.name)
                    result = self._run_tool(block)
                    self.ui.stop_spinner()
                    self.ui.tool_result(result["content"],
                                        error=result["is_error"])
                    results.append(result)
                messages.append({"role": "user", "content": results})
            self.ui.error("stopped — too many tool rounds in one turn")
            return "\n\n".join(said + ["(stopped — too many tool rounds "
                                       "in one turn)"])
        except KeyboardInterrupt:
            del messages[base:]
            self.ui.stop_spinner()
            print(file=sys.stderr)
            self.ui.notice("interrupted — this turn was rolled back")
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
