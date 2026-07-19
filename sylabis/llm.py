"""
LLM layer. Single entry point for all model calls.
--mock mode returns fixture responses so the full pipeline
is testable without an API key. This is not a toy feature:
it's how you write deterministic tests for a stochastic system.

WS1a reliability core:
- STAGE_MODELS routes stage families to models (fixtures stay keyed by
  the FULL stage name, so mock tests are unaffected by routing).
- .call() retries transient API failures (3 attempts, exponential
  backoff), raises typed errors (never SystemExit), detects max_tokens
  truncation, and logs per-stage token usage + cost to events.jsonl
  when `usage_dir` is set.
- .call_json() adds per-stage required-keys validation with ONE repair
  round; second failure raises SchemaError. The grader wave should use
  it too (validate_stage/STAGE_SCHEMAS cover audit/explain/tier3).
"""
import json
import time
from pathlib import Path
from dotenv import load_dotenv; load_dotenv()

from . import events
from .errors import ModelError, SchemaError, TruncationError

FIXTURES = Path(__file__).parent.parent / "fixtures"

# ---------------------------------------------------------------- models
# Stage names map to families by prefix: "lesson_00-intro" -> "lesson",
# "audit_02-x" -> "audit". One-shot generation stages run on Sonnet;
# high-volume cheap-judgment stages (claim audit, explain-back) run on
# Haiku. Tier-3 exemplar judging stays on Sonnet: it is the calibrated
# quality ceiling, not a bulk call.
SONNET = "claude-sonnet-4-6"
HAIKU = "claude-haiku-4-5-20251001"

STAGE_MODELS = {
    "intake": SONNET,
    "harvest": SONNET,
    "sequence": SONNET,
    "lesson": SONNET,
    "audit": HAIKU,
    "explain": HAIKU,
    "tier3": SONNET,
}
DEFAULT_MODEL = SONNET

# Deprecated alias — WS1a replaced the single constant with STAGE_MODELS.
# Kept so `from sylabis.llm import MODEL` keeps importing.
MODEL = SONNET

# USD per million tokens (input, output), keyed by model id.
PRICES = {
    SONNET: {"input": 3.00, "output": 15.00},
    HAIKU: {"input": 1.00, "output": 5.00},
}

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 1.0
_sleep = time.sleep  # module-level so tests can patch it


def stage_family(stage: str) -> str:
    """'lesson_00-data-audit' -> 'lesson'; 'intake' -> 'intake'."""
    return stage.split("_", 1)[0]


def stage_model(stage: str) -> str:
    return STAGE_MODELS.get(stage_family(stage), DEFAULT_MODEL)


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    return (input_tokens / 1e6) * p["input"] + (output_tokens / 1e6) * p["output"]


# ---------------------------------------------------------------- retries

def _retryable(exc: BaseException) -> bool:
    """Rate-limit / overload / network errors from the anthropic SDK."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover - anthropic is a runtime dep
        return False
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def retry_call(fn, what: str = "model call"):
    """Run fn with RETRY_ATTEMPTS attempts and exponential backoff on
    retryable API errors. Returns (result, retries_used). Non-retryable
    errors and exhausted retries raise ModelError — library code never
    raises SystemExit. Reusable by the agent wave (WS1b) for .chat()."""
    last_exc = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return fn(), attempt
        except Exception as exc:
            if not _retryable(exc):
                raise ModelError(f"{what} failed: {exc}") from exc
            last_exc = exc
            if attempt < RETRY_ATTEMPTS - 1:
                _sleep(RETRY_BASE_DELAY * (2 ** attempt))
    raise ModelError(
        f"{what} failed after {RETRY_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


# ------------------------------------------------------- output schemas
# Required-keys validation per stage family (stdlib only — WS4 migrates
# this to structured outputs). Spec values: None = key must be present;
# a type = key must be present and isinstance; a dict = nested object
# with its own required keys.
STAGE_SCHEMAS = {
    "intake": {"topic": None, "domain": None, "target_artifact": None,
               "viability": {"verdict": None, "grader_mode": None,
                             "verifiable_skeleton_pct": None, "notes": None}},
    "harvest": {"sources": list},
    "sequence": {"milestones": list},
    "audit": {"claims": list,
              "summary": {"total": None, "passed": None, "failed": None,
                          "flags": list, "blocking": None}},
    "explain": {"probes": list},
    "tier3": {"dimensions": list, "overall": None, "feedback": None},
}


def validate_stage(stage: str, data) -> list[str]:
    """Required-keys check for a one-shot stage's parsed output.
    Returns a list of problems; empty list means valid. Unknown stage
    families validate trivially (raw-markdown stages have no schema)."""
    schema = STAGE_SCHEMAS.get(stage_family(stage))
    if schema is None:
        return []
    if not isinstance(data, dict):
        return [f"top-level output must be a JSON object, "
                f"got {type(data).__name__}"]
    return _check_keys(schema, data, "")


def _check_keys(schema: dict, data: dict, prefix: str) -> list[str]:
    problems = []
    for key, spec in schema.items():
        path = f"{prefix}{key}"
        if key not in data:
            problems.append(f"missing required key: {path}")
            continue
        val = data[key]
        if isinstance(spec, dict):
            if not isinstance(val, dict):
                problems.append(f"{path} must be a JSON object")
            else:
                problems.extend(_check_keys(spec, val, path + "."))
        elif isinstance(spec, type) and not isinstance(val, spec):
            problems.append(f"{path} must be of type {spec.__name__}")
    return problems


def _parse_and_validate(stage: str, text: str):
    try:
        data = parse_json(text)
    except (json.JSONDecodeError, ValueError, IndexError) as e:
        return None, [f"output was not valid JSON: {e}"]
    return data, validate_stage(stage, data)


class LLM:
    def __init__(self, mock: bool = False, fixtures_dir: Path | str | None = None,
                 usage_dir: Path | str | None = None):
        self.mock = mock
        self.fixtures = Path(fixtures_dir) if fixtures_dir else FIXTURES
        # When set, every .call() appends an llm.usage event to
        # <usage_dir>/events.jsonl (zeroed tokens in mock mode, so
        # event-schema tests stay deterministic).
        self.usage_dir = Path(usage_dir) if usage_dir else None
        if not mock:
            from anthropic import Anthropic
            self.client = Anthropic()  # reads ANTHROPIC_API_KEY from env

    def call(self, stage: str, system: str, user: str, max_tokens: int = 4000) -> str:
        if self.mock:
            fixture = self.fixtures / f"{stage}.json"
            if not fixture.exists():
                raise FileNotFoundError(
                    f"Mock mode: no fixture for stage '{stage}' at {fixture}")
            text = fixture.read_text()
            self._log_usage(stage, stage_model(stage), 0, 0, 0)
            # Fixtures holding a bare JSON string (lesson stages) stand in
            # for raw model markdown — decode them. JSON-object fixtures
            # return raw text for parse_json, same as a real response.
            try:
                val = json.loads(text)
            except json.JSONDecodeError:
                return text
            return val if isinstance(val, str) else text

        model = stage_model(stage)
        resp, retries = retry_call(
            lambda: self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,  # one-shot stages are reproducible by design
                system=system,
                messages=[{"role": "user", "content": user}],
            ),
            what=f"stage {stage!r}")
        usage = getattr(resp, "usage", None)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        self._log_usage(stage, model, in_tok, out_tok, retries)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise TruncationError(
                f"stage {stage!r} hit max_tokens ({max_tokens}); refusing to "
                f"ship a silently truncated document")
        return "".join(b.text for b in resp.content if b.type == "text")

    def call_json(self, stage: str, system: str, user: str,
                  max_tokens: int = 4000) -> dict:
        """One-shot JSON stage: call -> parse -> required-keys check.
        On failure, re-prompt ONCE with the validation errors appended;
        a second failure raises SchemaError. Kills the bare-KeyError
        crash class on malformed model output."""
        text = self.call(stage, system, user, max_tokens)
        data, problems = _parse_and_validate(stage, text)
        if not problems:
            return data
        repair_user = (
            f"{user}\n\nYour previous output failed validation:\n"
            + "\n".join(f"- {p}" for p in problems)
            + "\nOutput ONLY the corrected JSON object, nothing else.")
        text = self.call(stage, system, repair_user, max_tokens)
        data, problems = _parse_and_validate(stage, text)
        if not problems:
            return data
        raise SchemaError(
            f"stage {stage!r} output failed schema validation after one "
            f"repair round: " + "; ".join(problems))

    def _log_usage(self, stage: str, model: str, input_tokens: int,
                   output_tokens: int, retries: int) -> None:
        if not self.usage_dir:
            return
        events.emit(self.usage_dir, "llm.usage", {
            "stage": stage,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(compute_cost(model, input_tokens, output_tokens), 6),
            "retries": retries,
        })

    def chat(self, system: str, messages: list, tools: list,
             max_tokens: int = 4000, on_text=None):
        """One turn of a tool-use conversation; returns the raw response
        (the agent loop needs stop_reason and tool_use blocks, not just
        text). Pass on_text to stream prose deltas as they generate — the
        terminal harness paints them live. No mock mode: the agent is a
        conversation with a live model; everything below it (compiler,
        grader, tools) mocks independently. The agent wave (WS1b) wraps
        this with retry_call() at the turn level — retrying a partially
        streamed turn here would duplicate streamed text."""
        if self.mock:
            raise RuntimeError("chat has no mock mode — test the tools, "
                               "not the conversation")
        if on_text is None:
            return self.client.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=messages, tools=tools)
        with self.client.messages.stream(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=messages, tools=tools) as stream:
            for delta in stream.text_stream:
                on_text(delta)
            return stream.get_final_message()


def parse_json(text: str) -> dict:
    """Strip markdown fences and parse. LLMs add fences no matter
    how firmly you tell them not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(t)
