"""
WS4 suite: structured outputs (4.1) and agentic harvest + parallel
lesson emission (4.2).

Fully offline and deterministic: model responses are fixtures; "live"
API paths use a fake anthropic-shaped client built locally; verification
network paths use httpx.MockTransport — zero network, no API key.
"""
import io
import json
import shutil
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

import httpx
import yaml

from sylabis import compiler
from sylabis import prompts
from sylabis import verify as verify_mod
from sylabis.compiler import (_Checkpoint, _consolidate_sources,
                              compile_course, self_test)
from sylabis.llm import LLM, STAGE_SCHEMAS
from sylabis.prompts import STAGE_OUTPUT_SCHEMAS, stage_output_schema
from sylabis.verify import resolve_locator, verify_sources

FIX = Path(__file__).parent.parent / "fixtures"


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


class _CountingLLM(LLM):
    """Mock LLM counting .call() invocations per stage."""

    def __init__(self, fixtures_dir: Path | None = None):
        super().__init__(mock=True, fixtures_dir=fixtures_dir)
        self.calls: dict[str, int] = {}

    def call(self, stage, system, user, max_tokens=4000):
        self.calls[stage] = self.calls.get(stage, 0) + 1
        return super().call(stage, system, user, max_tokens)


def compile_ws4(tmp: Path, llm: LLM, out: str = "course") -> Path:
    with redirect_stdout(io.StringIO()):
        return compile_course("Survey synthesis", {"weekly_hours": 5},
                              tmp / out, llm)


def events_of(course: Path) -> list[dict]:
    return [json.loads(line)
            for line in (course / "events.jsonl").read_text().splitlines()]


def status_of(course: Path) -> dict:
    return json.loads((course / ".compile" / "status.json").read_text())


# ------------------------------------------ fake live anthropic client

class _Text:
    type = "text"

    def __init__(self, t):
        self.text = t


class _Usage:
    input_tokens, output_tokens = 100, 50


class _Resp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_Text(text)]
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, script):
        self.script = list(script)
        self.kwargs = []

    def create(self, **kw):
        self.kwargs.append(kw)
        return self.script.pop(0)


class _FakeClient:
    def __init__(self, script):
        self.messages = _Messages(script)


def live_llm(script) -> LLM:
    llm = LLM(mock=True)  # avoid Anthropic() construction
    llm.mock = False
    llm.client = _FakeClient(script)
    return llm


# ------------------------------------------ WS4.1: structured outputs

def test_structured_output_requested_on_live_json_stages():
    llm = live_llm([_Resp("{}"), _Resp("# lesson")])
    llm.call("intake", "sys", "user")
    llm.call("lesson_00-data-audit", "sys", "user")
    kw = llm.client.messages.kwargs
    assert kw[0]["output_config"] == {
        "format": {"type": "json_schema", "schema": prompts.INTAKE_SCHEMA}}, \
        "JSON stages must request their schema via output_config.format"
    assert "output_config" not in kw[1], \
        "raw-markdown stages must not be schema-constrained"


def test_schemas_live_next_to_prompts_and_cover_json_stages():
    json_families = {"intake", "harvest", "sequence", "audit", "tier3",
                     "explain"}
    assert set(STAGE_OUTPUT_SCHEMAS) == json_families
    # the fallback required-keys table and the API schemas cover the
    # same stages — neither can drift silently
    assert set(STAGE_SCHEMAS) == json_families
    assert stage_output_schema("audit_00-x") is prompts.CLAIM_AUDIT_SCHEMA
    assert stage_output_schema("lesson_00-x") is None
    assert stage_output_schema("guide") is None
    for family, schema in STAGE_OUTPUT_SCHEMAS.items():
        assert schema["type"] == "object", family
        assert schema["required"], family
        assert schema["additionalProperties"] is False, family


def _validate(schema: dict, data, path="$") -> list[str]:
    """Mini JSON-Schema checker covering the subset STAGE_OUTPUT_SCHEMAS
    uses (type/required/properties/items/enum/anyOf/additionalProperties/
    minimum/maximum/maxLength). Stdlib only, tests only."""
    if "anyOf" in schema:
        branches = [_validate(s, data, path) for s in schema["anyOf"]]
        return [] if any(not b for b in branches) else \
            [f"{path}: no anyOf branch matched {data!r}"]
    if "enum" in schema:
        return [] if data in schema["enum"] else \
            [f"{path}: {data!r} not in enum"]
    errs = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected object"]
        for k in schema.get("required", []):
            if k not in data:
                errs.append(f"{path}.{k}: missing")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errs += [f"{path}.{k}: unexpected key" for k in data
                     if k not in props]
        for k, sub in props.items():
            if k in data:
                errs += _validate(sub, data[k], f"{path}.{k}")
    elif t == "array":
        if not isinstance(data, list):
            return [f"{path}: expected array"]
        for i, item in enumerate(data):
            errs += _validate(schema.get("items", {}), item, f"{path}[{i}]")
    elif t == "string":
        if not isinstance(data, str):
            errs.append(f"{path}: expected string")
        elif len(data) > schema.get("maxLength", len(data)):
            errs.append(f"{path}: exceeds maxLength")
    elif t in ("number", "integer"):
        ok = (isinstance(data, int) if t == "integer"
              else isinstance(data, (int, float)))
        if not ok or isinstance(data, bool):
            errs.append(f"{path}: expected {t}")
        else:
            if data < schema.get("minimum", data):
                errs.append(f"{path}: below minimum")
            if data > schema.get("maximum", data):
                errs.append(f"{path}: above maximum")
    elif t == "boolean":
        if not isinstance(data, bool):
            errs.append(f"{path}: expected boolean")
    elif t == "null":
        if data is not None:
            errs.append(f"{path}: expected null")
    return errs


def test_every_fixture_conforms_to_its_stage_schema():
    checked = 0
    for fixture in sorted(FIX.glob("*.json")):
        schema = stage_output_schema(fixture.stem)
        if schema is None:  # lesson fixtures are raw markdown strings
            continue
        problems = _validate(schema, json.loads(fixture.read_text()))
        assert not problems, f"{fixture.name}: {problems}"
        checked += 1
    assert checked >= 8, "the fixture set must exercise every JSON stage"


def test_mock_seam_unchanged_by_structured_outputs():
    llm = LLM(mock=True)
    spec = llm.call_json("intake", "sys", "user")
    assert spec["viability"]["verdict"] == "proceed"
    lesson = llm.call("lesson_00-data-audit", "sys", "user")
    assert isinstance(lesson, str) and lesson.strip()


# ------------------------------- WS4.2: concurrent locator verification

_ATOM_OK = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<feed xmlns="http://www.w3.org/2005/Atom">'
    '<entry><id>http://arxiv.org/abs/1503.02531v1</id>'
    '<title>Distilling the Knowledge in a Neural Network</title></entry>'
    '</feed>')


def _fake_net(counter: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        url = str(request.url)
        if url.startswith(verify_mod.ARXIV_API):
            return httpx.Response(200, text=_ATOM_OK)
        if "doi.org" in url:
            return httpx.Response(302,
                                  headers={"Location": "https://pub.test/x"})
        if "ok.test" in url:
            return httpx.Response(200)
        if "dead.test" in url:
            return httpx.Response(404)
        if "boom.test" in url:
            raise httpx.ConnectError("no route to host")
        return httpx.Response(500)
    return httpx.MockTransport(handler)


def _mixed_sources() -> list[dict]:
    return [
        {"id": "hinton", "locator": "arXiv:1503.02531"},
        {"id": "ghost-arxiv", "locator": "2401.99999"},
        {"id": "docs", "locator": "https://ok.test/docs"},
        {"id": "dead", "locator": "https://dead.test/gone"},
        {"id": "braun", "locator": "doi:10.1191/1478088706qp063oa"},
        {"id": "flaky", "locator": "https://boom.test/x"},
        {"id": "unsure", "locator": "search: fowler methods"},
        {"id": "book", "locator": "Fowler 5th edition"},
    ]


def test_concurrent_verification_matches_serial():
    serial, concurrent = _mixed_sources(), _mixed_sources()
    old = verify_mod.MAX_CONCURRENCY
    try:
        verify_mod.MAX_CONCURRENCY = 1  # the old serial path
        sum_serial = verify_sources(serial, transport=_fake_net())
        verify_mod.MAX_CONCURRENCY = 8
        sum_conc = verify_sources(concurrent, transport=_fake_net())
    finally:
        verify_mod.MAX_CONCURRENCY = old
    assert sum_serial == sum_conc == {"verified": 3, "unverified": 3,
                                      "flagged_search": 1, "skipped": 1}
    assert serial == concurrent, \
        "per-source annotations must not depend on concurrency"
    by_id = {s["id"]: s for s in concurrent}
    assert by_id["hinton"]["verified"] is True
    assert by_id["ghost-arxiv"]["verification"] == "arxiv_api_not_found"
    assert by_id["dead"]["verification"] == "http_404"
    assert by_id["braun"]["verification"] == "doi_registered"
    assert by_id["flaky"]["verification"].startswith("network_error")
    assert by_id["unsure"]["verified"] is False, "search stays flagged"
    assert by_id["book"]["verified"] is None


def test_verification_cache_stops_rehitting_hosts():
    counter = {"n": 0}
    cache: dict = {}
    first = _mixed_sources()
    summary1 = verify_sources(first, cache=cache,
                              transport=_fake_net(counter))
    hits_first = counter["n"]
    assert hits_first == 5, "1 arxiv batch + 4 URL checks"
    assert set(cache) == {"arXiv:1503.02531", "2401.99999",
                          "https://ok.test/docs", "https://dead.test/gone",
                          "doi:10.1191/1478088706qp063oa"}, \
        "network errors must NOT be cached — a resume should re-check them"
    second = _mixed_sources()
    summary2 = verify_sources(second, cache=cache,
                              transport=_fake_net(counter))
    assert counter["n"] == hits_first + 1, \
        "a cached locator must never re-hit its host (only flaky re-checks)"
    assert summary1 == summary2
    assert [(s["verified"], s["verification"]) for s in second] == \
           [(s["verified"], s["verification"]) for s in first]


def test_verify_still_flags_and_continues_when_disabled():
    sources = [{"locator": "doi:10.1/x"}, {"locator": "search: foo"}]
    summary = verify_sources(sources, enabled=False, cache={})
    assert summary == {"verified": 0, "unverified": 0,
                       "flagged_search": 1, "skipped": 1}


# --------------------------------- WS4.2: harvest consolidation + floor

def _extra_source(sid: str, locator: str) -> dict:
    return {"id": sid, "title": sid, "author": "A", "year": 2020,
            "type": "docs", "locator": locator, "authority": 0.8,
            "what_learner_needs": "the basics",
            "okf_description": f"Fixture source {sid}.",
            "freshness_class": "stable"}


def _fake_verify(fail_ids: set):
    """Stands in for compiler.verify_sources: annotates like the real one
    (search flagged, opaque skipped) but resolvable locators verify per
    the table instead of the network. Same signature, same summary."""
    def fake(sources, enabled=True, cache=None, transport=None):
        summary = {"verified": 0, "unverified": 0, "flagged_search": 0,
                   "skipped": 0}
        for s in sources:
            _, kind = resolve_locator(s.get("locator", ""))
            s["locator_kind"] = kind
            if kind == "search":
                s["verified"] = False
                s["verification"] = "search_locator_model_unsure"
                summary["flagged_search"] += 1
            elif kind == "opaque":
                s["verified"] = None
                s["verification"] = "locator_not_checkable"
                summary["skipped"] += 1
            else:
                ok = s["id"] not in fail_ids
                s["verified"] = ok
                s["verification"] = "http_200" if ok else "http_404"
                summary["verified" if ok else "unverified"] += 1
        return summary
    return fake


def _shipped_ids(course: Path) -> set:
    harvest = yaml.safe_load((course / "grader" / "sources.yaml").read_text())
    return {s["id"] for s in harvest["sources"]}


def test_harvest_ships_only_verified_or_flagged_and_logs_drops(tmp):
    base = json.loads((FIX / "harvest.json").read_text())
    mix = {"sources": deepcopy(base["sources"]) + [
        _extra_source("good-docs", "https://ok.test/docs"),
        _extra_source("dead-link", "https://dead.test/gone")]}
    fdir = make_fixtures(tmp, {"harvest": mix})
    old = compiler.verify_sources
    compiler.verify_sources = _fake_verify(fail_ids={"dead-link"})
    try:
        course = compile_ws4(tmp, LLM(mock=True, fixtures_dir=fdir))
    finally:
        compiler.verify_sources = old
    # verified (braun-clarke, good-docs) and flagged-search (fowler) ship;
    # the failed resolvable locator is dropped before sequencing
    assert _shipped_ids(course) == {"fowler", "braun-clarke", "good-docs"}
    assert not (course / "knowledge" / "source-dead-link.md").exists()
    assert (course / "knowledge" / "source-good-docs.md").exists()
    dropped = [e for e in events_of(course) if e["type"] == "harvest.dropped"]
    assert len(dropped) == 1
    p = dropped[0]["payload"]
    assert p["source_id"] == "dead-link"
    assert p["verification"] == "http_404" and p["locator_kind"] == "http"
    assert not [e for e in events_of(course)
                if e["type"] == "compile.warning"]
    assert self_test(course) == [], "the consolidated bundle must be whole"
    assert (course / ".compile" / "verify_cache.json").exists()


def test_drop_floor_keeps_sources_and_warns(tmp):
    thin = {"sources": [_extra_source("good-docs", "https://ok.test/docs"),
                        _extra_source("dead-1", "https://dead.test/a"),
                        _extra_source("dead-2", "https://dead.test/b")]}
    fdir = make_fixtures(tmp, {"harvest": thin})
    old = compiler.verify_sources
    compiler.verify_sources = _fake_verify(fail_ids={"dead-1", "dead-2"})
    try:
        course = compile_ws4(tmp, LLM(mock=True, fixtures_dir=fdir))
    finally:
        compiler.verify_sources = old
    # 2 of 3 would drop (>50%) — warn and keep them flagged instead
    assert _shipped_ids(course) == {"good-docs", "dead-1", "dead-2"}
    assert not [e for e in events_of(course)
                if e["type"] == "harvest.dropped"]
    warnings = [e["payload"] for e in events_of(course)
                if e["type"] == "compile.warning"]
    floor = [w for w in warnings if w["reason"] == "verification_drop_floor"]
    assert len(floor) == 1 and floor[0]["stage"] == "harvest"
    assert set(floor[0]["unverifiable"]) == {"dead-1", "dead-2"}
    # the base sequence fixture references "fowler", which this harvest
    # cannot ship — the milestone-level floor fires at sequencing time
    assert any(w["reason"] == "milestone_source_floor"
               and w["missing_source_ids"] == ["fowler"]
               for w in warnings)
    # kept-but-flagged sources are visibly marked in the knowledge bundle
    doc = (course / "knowledge" / "source-dead-1.md").read_text()
    assert "NO — http_404" in doc


def test_search_hook_seam_rescues_or_drops(tmp):
    def harvest_of(*sources):
        return {"sources": [dict(s) for s in sources]}
    ok = {"id": "ok", "locator": "https://ok.test/a",
          "locator_kind": "http", "verified": True, "verification": "http_200"}
    ok2 = dict(ok, id="ok2", locator="https://ok.test/b")
    bad = {"id": "bad", "locator": "https://dead.test/x",
           "locator_kind": "http", "verified": False,
           "verification": "http_404"}

    calls = []

    def reverify(source):  # the corrected locator verifies fine
        source["verified"] = True
        source["verification"] = "http_200"

    def hook(source):
        calls.append(source["id"])
        return "https://ok.test/corrected"

    old = compiler.SEARCH_LOCATOR_HOOK
    compiler.SEARCH_LOCATOR_HOOK = hook
    try:
        with redirect_stdout(io.StringIO()):
            out = _consolidate_sources(harvest_of(ok, ok2, bad), tmp, reverify)
    finally:
        compiler.SEARCH_LOCATOR_HOOK = old
    assert calls == ["bad"], "hook runs only for failed resolvable locators"
    rescued = {s["id"]: s for s in out["sources"]}
    assert set(rescued) == {"ok", "ok2", "bad"}
    assert rescued["bad"]["locator"] == "https://ok.test/corrected"
    assert not (tmp / "events.jsonl").exists(), "a rescue is not a drop"

    # hook declines (returns None) -> the normal drop path runs
    old = compiler.SEARCH_LOCATOR_HOOK
    compiler.SEARCH_LOCATOR_HOOK = lambda source: None
    try:
        with redirect_stdout(io.StringIO()):
            out = _consolidate_sources(harvest_of(ok, ok2, bad), tmp,
                                       reverify)
    finally:
        compiler.SEARCH_LOCATOR_HOOK = old
    assert {s["id"] for s in out["sources"]} == {"ok", "ok2"}
    dropped = [json.loads(line)
               for line in (tmp / "events.jsonl").read_text().splitlines()]
    assert [e["payload"]["source_id"] for e in dropped
            if e["type"] == "harvest.dropped"] == ["bad"]


# ------------------------------------ WS4.2: parallel lesson emission

def test_resume_mid_parallel_lessons_repeats_no_completed_lesson(tmp):
    broken = make_fixtures(tmp, remove=("lesson_01-quant-skeleton",))
    spy1 = _CountingLLM(broken)
    try:
        compile_ws4(tmp, spy1)
    except FileNotFoundError:
        pass  # simulated crash inside the lesson pool
    else:
        raise AssertionError("missing lesson fixture must abort the compile")
    st = status_of(tmp / "course")
    assert st["current"] == "lesson_01-quant-skeleton", \
        "on a pool failure, current must point at the failed lesson"
    assert "lesson_01-quant-skeleton" in st["error"]
    assert "lesson_00-data-audit" in st["done"], \
        "the lesson that finished must stay checkpointed"
    assert spy1.calls["lesson_00-data-audit"] == 1

    spy2 = _CountingLLM()  # full fixtures — the re-run
    course = compile_ws4(tmp, spy2)
    assert spy2.calls == {"lesson_01-quant-skeleton": 1}, \
        "resume must repeat no completed lesson and no earlier stage"
    st = status_of(course)
    assert st["current"] is None and st["error"] is None
    assert st["done"] == st["stages"], "done stays normalized to stage order"
    assert self_test(course) == []


def test_parallel_emission_is_deterministic(tmp):
    a = compile_ws4(tmp, LLM(mock=True), out="a")
    b = compile_ws4(tmp, LLM(mock=True), out="b")
    for mid in ("00-data-audit", "01-quant-skeleton"):
        assert (a / mid / "LESSON.md").read_text() == \
               (b / mid / "LESSON.md").read_text(), \
            "pool completion order must not leak into the bundle"
    docs_a = [d["path"] for d in yaml.safe_load(
        (a / "okf.yaml").read_text())["documents"]]
    docs_b = [d["path"] for d in yaml.safe_load(
        (b / "okf.yaml").read_text())["documents"]]
    assert docs_a == docs_b
    ms_a = yaml.safe_load((a / "course.yaml").read_text())["milestones"]
    ms_b = yaml.safe_load((b / "course.yaml").read_text())["milestones"]
    assert ms_a == ms_b


def test_run_parallel_status_representation_and_exactly_once(tmp):
    ck = _Checkpoint(tmp / "c")
    ck.set_stages(["lesson_a", "lesson_b", "emit"])
    seen = {}

    def job(name, value):
        def fn():
            seen[name] = json.loads(
                (tmp / "c" / ".compile" / "status.json").read_text())["current"]
            return value
        return fn

    out = ck.run_parallel([("lesson_a", job("a", "A")),
                           ("lesson_b", job("b", "B"))],
                          workers=2, label="lessons")
    assert out == {"lesson_a": "A", "lesson_b": "B"}
    assert seen == {"a": "lessons", "b": "lessons"}, \
        "while the pool runs, current is the 'lessons' pseudo-stage"
    st = json.loads((tmp / "c" / ".compile" / "status.json").read_text())
    assert st["current"] is None
    assert st["done"] == ["lesson_a", "lesson_b"], \
        "done is normalized to stages order regardless of completion order"

    def boom():
        raise AssertionError("a completed stage must never rerun")

    again = ck.run_parallel([("lesson_a", boom), ("lesson_b", boom)],
                            workers=2, label="lessons")
    assert again == out, "second run loads results from disk"
