"""
LLM layer. Single entry point for all model calls.
--mock mode returns fixture responses so the full pipeline
is testable without an API key. This is not a toy feature:
it's how you write deterministic tests for a stochastic system.
"""
import json
import os
from pathlib import Path

MODEL = "claude-sonnet-4-6"
FIXTURES = Path(__file__).parent.parent / "fixtures"


class LLM:
    def __init__(self, mock: bool = False):
        self.mock = mock
        if not mock:
            from anthropic import Anthropic
            self.client = Anthropic()  # reads ANTHROPIC_API_KEY from env

    def call(self, stage: str, system: str, user: str, max_tokens: int = 4000) -> str:
        if self.mock:
            fixture = FIXTURES / f"{stage}.json"
            if fixture.exists():
                return fixture.read_text()
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


def parse_json(text: str) -> dict:
    """Strip markdown fences and parse. LLMs add fences no matter
    how firmly you tell them not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(t)
