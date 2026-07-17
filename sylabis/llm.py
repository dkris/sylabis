"""
LLM layer. Single entry point for all model calls.
--mock mode returns fixture responses so the full pipeline
is testable without an API key. This is not a toy feature:
it's how you write deterministic tests for a stochastic system.
"""
import json
import os
from pathlib import Path

from . import config

config.load_env()

MODEL = config.DEFAULT_MODEL  # compat alias; LLM instances use config.model()
FIXTURES = Path(__file__).parent.parent / "fixtures"

_NO_KEY = ("sylabis talks through Claude and no API key is set.\n"
           "Run `sy init` to save one (get a key at "
           "https://console.anthropic.com/settings/keys).")


class LLM:
    def __init__(self, mock: bool = False, fixtures_dir: Path | str | None = None):
        self.mock = mock
        self.fixtures = Path(fixtures_dir) if fixtures_dir else FIXTURES
        self.model = config.model()
        if not mock:
            # Checked here — the one place clients are built — so every
            # command fails the same friendly way, never with a traceback.
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise SystemExit(_NO_KEY)
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
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")

    def chat(self, system: str, messages: list, tools: list,
             max_tokens: int = 4000, on_text=None):
        """One turn of a tool-use conversation; returns the raw response
        (the agent loop needs stop_reason and tool_use blocks, not just
        text). Pass on_text to stream prose deltas as they generate — the
        terminal harness paints them live. No mock mode: the agent is a
        conversation with a live model; everything below it (compiler,
        grader, tools) mocks independently."""
        if self.mock:
            raise RuntimeError("chat has no mock mode — test the tools, "
                               "not the conversation")
        if on_text is None:
            return self.client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=messages, tools=tools)
        with self.client.messages.stream(
                model=self.model, max_tokens=max_tokens, system=system,
                messages=messages, tools=tools) as stream:
            for delta in stream.text_stream:
                on_text(delta)
            return stream.get_final_message()


def validate_key() -> str | None:
    """One tiny live call to prove the saved key actually works — the only
    intentionally-networked function in this module; only `sy init` calls
    it. Returns None on success, a friendly one-line diagnosis otherwise."""
    import anthropic
    try:
        anthropic.Anthropic().messages.create(
            model=config.model(), max_tokens=1,
            messages=[{"role": "user", "content": "ping"}])
        return None
    except anthropic.AuthenticationError:
        return ("The key was rejected (authentication failed). Check it at "
                "https://console.anthropic.com/settings/keys and re-run "
                "`sy init`.")
    except anthropic.NotFoundError:
        return (f"The key works but model {config.model()!r} was not found — "
                "set SYLABIS_MODEL to an available model id.")
    except anthropic.APIConnectionError:
        return ("Could not reach the API (network problem?). The key is "
                "saved; try `sy` once you are online.")
    except Exception as e:  # a validation nicety must never crash setup
        return f"Could not validate the key ({e.__class__.__name__}: {e})."


def parse_json(text: str) -> dict:
    """Strip markdown fences and parse. LLMs add fences no matter
    how firmly you tell them not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(t)
