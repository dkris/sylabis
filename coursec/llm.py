"""
LLM layer. Single entry point for all model calls.
--mock mode returns fixture responses so the full pipeline
is testable without an API key. This is not a toy feature:
it's how you write deterministic tests for a stochastic system.
"""
import json
import os
from pathlib import Path
from dotenv import load_dotenv; load_dotenv()

MODEL = "claude-sonnet-4-6"
FIXTURES = Path(__file__).parent.parent / "fixtures"


class LLM:
    def __init__(self, mock: bool = False, fixtures_dir: Path | str | None = None):
        self.mock = mock
        self.fixtures = Path(fixtures_dir) if fixtures_dir else FIXTURES
        if not mock:
            from anthropic import Anthropic
            self.client = Anthropic()  # reads ANTHROPIC_API_KEY from env

    def call(self, stage: str, system: str, user: str, max_tokens: int = 4000) -> str:
        if self.mock:
            fixture = self.fixtures / f"{stage}.json"
            if fixture.exists():
                text = fixture.read_text()
                # Fixtures holding a bare JSON string (lesson stages) stand in
                # for raw model markdown — decode them. JSON-object fixtures
                # return raw text for parse_json, same as a real response.
                try:
                    val = json.loads(text)
                except json.JSONDecodeError:
                    return text
                return val if isinstance(val, str) else text
            raise FileNotFoundError(
                f"Mock mode: no fixture for stage '{stage}' at {fixture}"
            )
        resp = self.client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    def chat(self, system: str, messages: list, tools: list,
             max_tokens: int = 4000):
        """One turn of a tool-use conversation; returns the raw response
        (the agent loop needs stop_reason and tool_use blocks, not just
        text). No mock mode: the agent is a conversation with a live model;
        everything below it (compiler, grader, tools) mocks independently."""
        if self.mock:
            raise RuntimeError("chat has no mock mode — test the tools, "
                               "not the conversation")
        return self.client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=messages, tools=tools)


def parse_json(text: str) -> dict:
    """Strip markdown fences and parse. LLMs add fences no matter
    how firmly you tell them not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(t)
