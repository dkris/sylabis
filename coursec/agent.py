"""
The agent harness. `coursec` with no arguments lands here: a conversation
in the terminal where the model drives the whole learn → submit → grade →
adapt loop through the journey tools. The harness is deliberately thin —
a read line, a tool-use loop, a printed reply. Everything that matters
lives in the tools (tools.py) and the guide prompt (prompts.py); this
file should never grow logic of its own.
"""
import sys
from pathlib import Path

from .llm import LLM
from .prompts import GUIDE_SYSTEM
from .tools import JourneyTools, ToolError

MAX_TOOL_ROUNDS = 12  # per user turn; a guide that needs more is looping


class Agent:
    def __init__(self, home_dir: Path, mock: bool = False, llm: LLM | None = None):
        self.tools = JourneyTools(home_dir, mock=mock)
        self.llm = llm or LLM(mock=mock)
        self.specs = [{"name": n, "description": d, "input_schema": s}
                      for n, (_, d, s) in self.tools.registry.items()]

    def run(self) -> None:
        print("coursec — your learning journey. Empty line or Ctrl-D to leave.")
        messages: list[dict] = []
        greeted = False
        while True:
            try:
                user = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not user:
                return
            if not greeted:
                # First turn carries the standing instruction to orient.
                user = f"(new session — orient first)\n{user}"
                greeted = True
            messages.append({"role": "user", "content": user})
            self.turn(messages)

    def turn(self, messages: list[dict]) -> None:
        """One user turn: call the model, run any tools it asks for, feed
        results back, repeat until it answers in text."""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.llm.chat(GUIDE_SYSTEM, messages, self.specs)
            messages.append({"role": "assistant",
                             "content": [b.model_dump() for b in resp.content]})
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    print(f"\n{block.text.strip()}")
            if resp.stop_reason != "tool_use":
                return
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                print(f"  [{block.name}]", file=sys.stderr)
                results.append(self._run_tool(block))
            messages.append({"role": "user", "content": results})
        print("\n(agent stopped — too many tool rounds in one turn)")

    def _run_tool(self, block) -> dict:
        try:
            text, is_error = self.tools.call(block.name, block.input), False
        except ToolError as e:
            text, is_error = str(e), True
        except Exception as e:  # a tool bug is self-correctable, not fatal
            text, is_error = f"Tool error: {e}", True
        return {"type": "tool_result", "tool_use_id": block.id,
                "content": text, "is_error": is_error}
