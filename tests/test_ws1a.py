"""
WS1a reliability-core suite: retries + typed errors, schema-checked
model output with one repair round, compile checkpointing/resume,
truncation detection + cost log, STAGE_MODELS routing, provenance
stamps, and the compile.requested learner-profile-hash privacy fix.

Fully offline and deterministic: model responses are fixtures; "live"
API paths use a fake anthropic client raising real anthropic exception
objects constructed locally (no network, no API key).
"""
import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from sylabis import __version__
from sylabis import llm as llm_mod
from sylabis.compiler import compile_course, profile_hash
from sylabis.errors import (CompileDeclined, ModelError, SchemaError,
                            TruncationError)
from sylabis.llm import (HAIKU, LLM, MODEL, SONNET, compute_cost,
                         stage_model, validate_stage)

FIX = Path(__file__).parent.parent / "fixtures"
PROFILE = {"weekly_hours": 5, "hardware": "m3-max-96gb"}
MILESTONE_IDS = ["00-data-audit", "01-quant-skeleton"]


# -------------------------------------------------------------- helpers

def make_fixtures(tmp: Path, overlays: dict | None = None,
                  remove: tuple = ()) -> Path:
    fdir = tmp / "fixtures"
    shutil.copytree(FIX, fdir)
    for stage, content in (overlays or {}).items():
        (fdir / f"{stage}.json").write_text(json.dumps(content))
    for stage in remove:
        (fdir / f"{stage}.json").unlink()
    return fdir


class SpyLLM(LLM):
    """Mock LLM counting .call() invocations per stage — the meter for
    'earlier stages' paid calls are NOT repeated'."""

    def __init__(self, fixtures_dir: Path | None = None):
        super().__init__(mock=True, fixtures_dir=fixtures_dir)
        self.calls: dict[str, int] = {}

    def call(self, stage, system, user, max_tokens=4000):
        self.calls[stage] = self.calls.get(stage, 0) + 1
        return super().call(stage, system, user, max_tokens)


def compile_ws(tmp: Path, llm: LLM, out: str = "course") -> Path:
    with redirect_stdout(io.StringIO()):
        return compile_course("Survey synthesis", dict(PROFILE),
                              tmp / out, llm)


def events_of(course: Path) -> list[dict]:
    return [json.loads(l)
            for l in (course / "events.jsonl").read_text().splitlines()]


def status_of(course: Path) -> dict:
    return json.loads((course / ".compile" / "status.json").read_text())


# --------------------------------------------- fake live anthropic client

class _Usage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class _Text:
    type = "text"

    def __init__(self, t):
        self.text = t


class _Resp:
    def __init__(self, text, stop_reason="end_turn", usage=(100, 50)):
        self.content = [_Text(text)]
        self.stop_reason = stop_reason
        self.usage = _Usage(*usage)


class _Messages:
    def __init__(self, script):
        self.script = list(script)
        self.kwargs = []

    def create(self, **kw):
        self.kwargs.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, script):
        self.messages = _Messages(script)


def live_llm(script, usage_dir: Path | None = None) -> LLM:
    """A non-mock LLM whose client is a local fake — exercises the real
    retry/truncation/usage path with zero network."""
    llm = LLM(mock=True)  # avoid Anthropic() construction
    llm.mock = False
    llm.client = _FakeClient(script)
    llm.usage_dir = usage_dir
    return llm


class _PatchedSleep:
    def __enter__(self):
        self.sleeps = []
        self._old = llm_mod._sleep
        llm_mod._sleep = self.sleeps.append
        return self.sleeps

    def __exit__(self, *exc):
        llm_mod._sleep = self._old


def _conn_error():
    import anthropic
    import httpx
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "http://offline.test"))


def _rate_limit():
    import anthropic
    import httpx
    req = httpx.Request("POST", "http://offline.test")
    return anthropic.RateLimitError(
        "rate limited", response=httpx.Response(429, request=req), body=None)


def _bad_request():
    import anthropic
    import httpx
    req = httpx.Request("POST", "http://offline.test")
    return anthropic.BadRequestError(
        "bad request", response=httpx.Response(400, request=req), body=None)


# ------------------------------------------------- retries + typed errors

def test_retry_recovers_and_logs_retry_count(tmp):
    with _PatchedSleep() as sleeps:
        llm = live_llm([_conn_error(), _rate_limit(), _Resp("ok")],
                       usage_dir=tmp)
        assert llm.call("intake", "sys", "user") == "ok"
    assert sleeps == [1.0, 2.0], "exponential backoff between 3 attempts"
    usage = [e for e in events_of(tmp) if e["type"] == "llm.usage"]
    assert len(usage) == 1
    p = usage[0]["payload"]
    assert p["retries"] == 2, "cumulative retry count must reach the cost log"
    assert p["input_tokens"] == 100 and p["output_tokens"] == 50


def test_retry_exhausted_raises_model_error(tmp):
    with _PatchedSleep() as sleeps:
        llm = live_llm([_conn_error(), _conn_error(), _conn_error()])
        try:
            llm.call("intake", "sys", "user")
        except ModelError as e:
            assert "3 attempts" in str(e)
        else:
            raise AssertionError("exhausted retries must raise ModelError")
    assert len(sleeps) == 2, "no sleep after the final attempt"


def test_non_retryable_error_fails_fast():
    with _PatchedSleep() as sleeps:
        llm = live_llm([_bad_request(), _Resp("never reached")])
        try:
            llm.call("intake", "sys", "user")
        except ModelError:
            pass
        else:
            raise AssertionError("4xx must raise ModelError, not retry")
    assert sleeps == [] and len(llm.client.messages.kwargs) == 1, \
        "a non-retryable error must not be retried"


def test_truncation_detected_and_usage_still_logged(tmp):
    llm = live_llm([_Resp("partial doc", stop_reason="max_tokens")],
                   usage_dir=tmp)
    try:
        llm.call("lesson_00-data-audit", "sys", "user", max_tokens=64)
    except TruncationError as e:
        assert "max_tokens" in str(e)
    else:
        raise AssertionError("max_tokens cutoff must raise TruncationError")
    usage = [e for e in events_of(tmp) if e["type"] == "llm.usage"]
    assert len(usage) == 1, "tokens were spent — the cost log must show them"


def test_stage_models_temperature_and_cost():
    llm = live_llm([_Resp("a"), _Resp("b", usage=(1000, 200))])
    llm.call("intake", "sys", "user")
    llm.call("audit_00-data-audit", "sys", "user")
    kw = llm.client.messages.kwargs
    assert kw[0]["model"] == SONNET and kw[1]["model"] == HAIKU
    assert kw[0]["temperature"] == 0 and kw[1]["temperature"] == 0
    assert stage_model("lesson_02-x") == SONNET
    assert stage_model("explain_02-x") == HAIKU
    assert MODEL == SONNET, "deprecated alias must keep importing"
    assert abs(compute_cost(SONNET, 1000, 200)
               - (1000 / 1e6 * 3.00 + 200 / 1e6 * 15.00)) < 1e-12
    assert abs(compute_cost(HAIKU, 1000, 200)
               - (1000 / 1e6 * 1.00 + 200 / 1e6 * 5.00)) < 1e-12


# --------------------------------------- schema validation + repair round

def test_validate_stage_required_keys():
    good = json.loads((FIX / "intake.json").read_text())
    assert validate_stage("intake", good) == []
    missing = {k: v for k, v in good.items() if k != "viability"}
    problems = validate_stage("intake", missing)
    assert any("viability" in p for p in problems)
    nested = dict(good, viability={"verdict": "proceed"})
    problems = validate_stage("intake", nested)
    assert any("viability.grader_mode" in p for p in problems)
    assert validate_stage("harvest", {"sources": "not-a-list"}) \
        == ["sources must be of type list"]
    assert validate_stage("lesson_00-x", "raw markdown has no schema") == []
    assert validate_stage("audit_00-x", {"claims": []}) != []


def test_schema_repair_succeeds_on_second_try():
    class Scripted(LLM):
        def __init__(self, replies):
            super().__init__(mock=True)
            self.replies = list(replies)
            self.prompts = []

        def call(self, stage, system, user, max_tokens=4000):
            self.prompts.append(user)
            return self.replies.pop(0)

    good = (FIX / "intake.json").read_text()
    llm = Scripted(['{"topic": "only a topic"}', good])
    spec = llm.call_json("intake", "sys", "user")
    assert spec["viability"]["verdict"] == "proceed"
    assert len(llm.prompts) == 2
    assert "failed validation" in llm.prompts[1]
    assert "viability" in llm.prompts[1], \
        "the repair prompt must carry the concrete validation errors"


def test_missing_key_fixture_repairs_once_then_schema_error(tmp):
    intake = json.loads((FIX / "intake.json").read_text())
    del intake["viability"]
    fdir = make_fixtures(tmp, {"intake": intake})
    spy = SpyLLM(fdir)
    try:
        compile_ws(tmp, spy)
    except SchemaError as e:
        assert "intake" in str(e)
    else:
        raise AssertionError("second schema failure must raise SchemaError")
    assert spy.calls["intake"] == 2, "exactly one repair attempt"
    st = status_of(tmp / "course")
    assert st["current"] == "intake" and st["error"], \
        "status.json must record the failed stage"


# ----------------------------------------------- checkpointing + resume

def test_status_json_contract_after_success(tmp):
    course = compile_ws(tmp, SpyLLM())
    st = status_of(course)
    assert st["schema"] == 1
    expected = ["intake", "harvest", "sequence",
                *[f"lesson_{m}" for m in MILESTONE_IDS],
                "emit", "self_test"]
    assert st["stages"] == expected
    assert st["done"] == expected, "done lists all stages on success"
    assert st["current"] is None and st["error"] is None
    for stage in expected:
        assert (course / ".compile" / f"{stage}.json").exists(), stage
    # per-stage outputs round-trip
    spec = json.loads((course / ".compile" / "intake.json").read_text())
    assert spec["viability"]["verdict"] == "proceed"
    lesson = json.loads(
        (course / ".compile" / f"lesson_{MILESTONE_IDS[0]}.json").read_text())
    assert isinstance(lesson, str) and lesson.strip()


def test_crash_mid_lessons_resumes_without_repeating_paid_calls(tmp):
    broken = make_fixtures(tmp, remove=(f"lesson_{MILESTONE_IDS[1]}",))
    spy1 = SpyLLM(broken)
    try:
        compile_ws(tmp, spy1)
    except FileNotFoundError:
        pass  # simulated crash generating lesson 2
    else:
        raise AssertionError("missing lesson fixture must abort the compile")
    st = status_of(tmp / "course")
    assert st["error"] and f"lesson_{MILESTONE_IDS[1]}" in st["current"]
    assert spy1.calls[f"lesson_{MILESTONE_IDS[0]}"] == 1

    spy2 = SpyLLM()  # full fixtures — the re-run
    course = compile_ws(tmp, spy2)
    for stage in ("intake", "harvest", "sequence", f"lesson_{MILESTONE_IDS[0]}"):
        assert stage not in spy2.calls, f"{stage} must not be re-bought"
    assert spy2.calls == {f"lesson_{MILESTONE_IDS[1]}": 1}
    st = status_of(course)
    assert st["current"] is None and st["error"] is None
    assert st["done"] == st["stages"]
    types = [e["type"] for e in events_of(course)]
    assert types.count("compile.requested") == 1, "resume must not re-emit"
    assert types.count("compile.completed") == 1
    # and the resumed bundle is whole
    from sylabis.compiler import self_test
    assert self_test(course) == []


def test_rerun_of_completed_compile_is_a_noop(tmp):
    course = compile_ws(tmp, SpyLLM())
    spy = SpyLLM()
    again = compile_ws(tmp, spy)
    assert again == course
    assert spy.calls == {}, "a completed compile must make zero model calls"
    types = [e["type"] for e in events_of(course)]
    assert types.count("compile.completed") == 1


def test_intake_decline_is_typed_and_recorded(tmp):
    intake = json.loads((FIX / "intake.json").read_text())
    intake["viability"]["verdict"] = "decline"
    intake["viability"]["notes"] = "under 20% verifiable"
    fdir = make_fixtures(tmp, {"intake": intake})
    try:
        compile_ws(tmp, LLM(mock=True, fixtures_dir=fdir))
    except CompileDeclined as e:
        assert e.verdict == "decline"
        assert "under 20% verifiable" in e.notes
        assert "Declined" in str(e)
    else:
        raise AssertionError("decline must raise CompileDeclined")
    st = status_of(tmp / "course")
    assert st["current"] == "intake" and "Declined" in st["error"]


# --------------------------------------------------- cost log + privacy

def test_llm_usage_events_zeroed_and_schema_complete_in_mock(tmp):
    course = compile_ws(tmp, SpyLLM())
    usage = [e for e in events_of(course) if e["type"] == "llm.usage"]
    stages = [e["payload"]["stage"] for e in usage]
    assert stages == ["intake", "harvest", "sequence",
                      *[f"lesson_{m}" for m in MILESTONE_IDS]]
    for e in usage:
        assert e["schema"] == 1
        for key in ("id", "ts", "type", "payload"):
            assert key in e
        p = e["payload"]
        for key in ("stage", "model", "input_tokens", "output_tokens",
                    "cost_usd", "retries"):
            assert key in p, f"llm.usage payload missing {key}"
        assert p["input_tokens"] == 0 and p["output_tokens"] == 0
        assert p["cost_usd"] == 0.0 and p["retries"] == 0
        assert p["model"] == stage_model(p["stage"])


def test_compile_requested_records_profile_hash_not_profile(tmp):
    course = compile_ws(tmp, SpyLLM())
    req = next(e for e in events_of(course)
               if e["type"] == "compile.requested")
    assert req["payload"]["learner_profile_hash"] == profile_hash(PROFILE)
    assert "profile" not in req["payload"], "raw learner block must not leak"
    log_text = (course / "events.jsonl").read_text()
    assert PROFILE["hardware"] not in log_text, \
        "the hardware string must never reach the event log"
    # hash is stable across key ordering
    reordered = dict(reversed(list(PROFILE.items())))
    assert profile_hash(reordered) == profile_hash(PROFILE)
    assert profile_hash({"weekly_hours": 6}) != profile_hash(PROFILE)


# ------------------------------------------------------------ provenance

def test_provenance_stamped_into_course_yaml(tmp):
    course = compile_ws(tmp, SpyLLM())
    meta = yaml.safe_load((course / "course.yaml").read_text())["meta"]
    assert meta["version"] == __version__, "hardcoded 0.1.0 is gone"
    prov = meta["provenance"]
    assert set(prov) == {"intake", "harvest", "sequence", "lesson"}
    from sylabis.prompts import PROMPT_VERSIONS
    for family, stamp in prov.items():
        assert stamp["model"] == stage_model(family)
        assert stamp["prompt_version"] == PROMPT_VERSIONS[family]
        assert stamp["sylabis_version"] == __version__
