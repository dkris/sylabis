"""
Adversarial grader suite (brief: Testing approach, Layer 2). Deterministic,
offline, stdlib-only: every LLM response is a fixture; adversarial cases
overlay their own fixture files on top of fixtures/ in a temp dir.

Run from the repo root:  python -m tests.run_all
Exit code 0 only when every case passes.

The four canonical cases: (1) structurally incomplete -> Tier 1 blocks;
(2) overclaiming -> Tier 2 blocks, Tier 1 passes; (3) claim-clean but
quality-weak -> Tier 3 catches, Tiers 1-2 pass; (4) genuinely strong ->
all tiers pass. Everything after that attacks the seams.
"""
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from sylabis import journey  # noqa: E402
from sylabis import okf  # noqa: E402
from sylabis.compiler import compile_course, compile_remedial, self_test, _check_dag  # noqa: E402
from sylabis.grader import grade  # noqa: E402
from sylabis.llm import LLM, parse_json  # noqa: E402
from sylabis.mcp_server import MCPServer  # noqa: E402
from sylabis.path_engine import decide, actuate  # noqa: E402
from sylabis.tools import JourneyTools, ToolError  # noqa: E402
from sylabis.verify import resolve_locator, verify_sources  # noqa: E402

FIX = Path(__file__).parent.parent / "fixtures"

STRONG_ARTIFACT = ("61% of respondents (n=140) reported satisfaction. "
                   "Findings cannot be generalized to SMB customers.")
STRONG_REFLECTION = ("I wanted the data to support more than it can; "
                     "the skew is a generalization boundary.")


def make_fixtures(tmp: Path, overlays: dict) -> Path:
    fdir = tmp / "fixtures"
    shutil.copytree(FIX, fdir)
    for stage, content in overlays.items():
        (fdir / f"{stage}.json").write_text(json.dumps(content))
    return fdir


def compile_mock(tmp: Path, fdir: Path | None = None) -> Path:
    out = tmp / "course"
    with redirect_stdout(io.StringIO()):
        compile_course("Survey synthesis", {"weekly_hours": 5}, out,
                       LLM(mock=True, fixtures_dir=fdir or FIX))
    return out


def submit(course: Path, mid: str, artifact: str | None,
           reflection: str | None) -> None:
    if artifact is not None:
        (course / mid / "artifact.md").write_text(artifact)
    if reflection is not None:
        (course / mid / "reflection.md").write_text(reflection)


def grade_mock(course: Path, mid: str, fdir: Path | None = None, **kw) -> dict:
    with redirect_stdout(io.StringIO()):
        return grade(course, mid, LLM(mock=True, fixtures_dir=fdir or FIX), **kw)


def event_types(course: Path) -> list[str]:
    return [json.loads(l)["type"]
            for l in (course / "events.jsonl").read_text().splitlines()]


def seed_exemplars(course: Path, mid: str) -> None:
    ex = course / "grader" / "exemplars" / mid
    ex.mkdir(parents=True)
    (ex / "strong-1.md").write_text(
        "---\ncategory: strong\nscore: 0.95\n"
        "reasoning: Boundaries as implications.\n---\nStrong body.\n")
    (ex / "weak-1.md").write_text(
        "---\ncategory: weak\nscore: 0.35\n"
        "reasoning: Restates skew as description.\n---\nWeak body.\n")
    cp_path = course / mid / "checkpoint.yaml"
    cp = yaml.safe_load(cp_path.read_text())
    cp["tier_3_rubric"]["enabled"] = True
    cp_path.write_text(yaml.dump(cp))


# ------------------------------------------------------ the four canon cases

def case_1_structurally_incomplete_tier1_blocks(tmp):
    course = compile_mock(tmp)
    submit(course, "00-data-audit", None, "reflection only")  # no artifact.md
    r = grade_mock(course, "00-data-audit")
    assert not r.get("tier_1_passed"), "Tier 1 must block"
    assert r["grade"] == 0.0 and not r["passed"]
    assert any(f.startswith("missing_file:artifact.md")
               for f in r["failure_flags"])
    assert "claim_audit" not in r, "Tier 2 must not run after a Tier 1 block"


def case_2_overclaiming_tier2_blocks(tmp):
    audit = {"claims": [
        {"text": "The redesign proves onboarding drives churn", "type": "causal",
         "evidence": "no experimental design", "result": "fail",
         "flag": "unsupported_causal", "feedback": "hedge or run an experiment"},
        {"text": "87.3% prefer the new flow", "type": "descriptive",
         "evidence": "no n reported", "result": "fail",
         "flag": "missing_n", "feedback": "report the denominator"}],
        "summary": {"total": 2, "passed": 0, "failed": 2,
                    "flags": ["unsupported_causal", "missing_n"],
                    "blocking": True}}
    fdir = make_fixtures(tmp, {"audit_00-data-audit": audit})
    course = compile_mock(tmp, fdir)
    submit(course, "00-data-audit", "The redesign proves onboarding drives churn. 87.3% prefer the new flow.", "reflection")
    r = grade_mock(course, "00-data-audit", fdir)
    assert r.get("tier_1_passed"), "Tier 1 must pass"
    assert not r.get("tier_2_passed") and not r["passed"]
    assert "unsupported_causal" in r["failure_flags"]
    assert "proves onboarding" in r["feedback"], "feedback must quote the claim"
    assert "explain_back" not in r, "explain-back not reached on Tier 2 block (prototype)"


def case_3_quality_weak_tier3_catches(tmp):
    tier3 = {"dimensions": [
        {"name": "artifact_fidelity", "score": 0.5,
         "nearest_exemplar": "weak",
         "rationale": "Sections present but limitations restate skew."}],
        "overall": 0.55,
        "feedback": "Rewrite each limitation as a generalization boundary."}
    fdir = make_fixtures(tmp, {"tier3_00-data-audit": tier3})
    course = compile_mock(tmp, fdir)
    seed_exemplars(course, "00-data-audit")
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    r = grade_mock(course, "00-data-audit", fdir)
    assert r.get("tier_1_passed") and r.get("tier_2_passed"), "Tiers 1-2 pass"
    assert r["tier_3_mode"] == "exemplar_rubric"
    assert r["grade"] == 0.55 and not r["passed"], "Tier 3 must catch it"


def case_4_strong_passes_all_tiers(tmp):
    course = compile_mock(tmp)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    r = grade_mock(course, "00-data-audit")
    assert r.get("tier_1_passed") and r.get("tier_2_passed") and r["passed"]
    assert r["verified_claims"]
    claim = course / "portfolio" / "claims" / "00-data-audit.md"
    assert claim.exists(), "competency-evidence doc must be emitted"
    meta, _ = okf.parse_doc(claim)
    assert meta["type"] == "competency-evidence" and meta["grade"] == r["grade"]
    idx_meta, _ = okf.parse_doc(course / "portfolio" / "index.md")
    assert idx_meta["claim_count"] == 1


# --------------------------------------------------------------- grader seams

def test_explain_back_caps_grade(tmp):
    explain = {"probes": [
        {"concept": "limitation vs description", "verdict": "misconception",
         "evidence": "learner restates skew as if it were a limitation",
         "followup_question": "What may a stakeholder NOT do with this finding?"}],
        "grade_cap": 0.7}
    fdir = make_fixtures(tmp, {"explain_00-data-audit": explain})
    course = compile_mock(tmp, fdir)
    submit(course, "00-data-audit", STRONG_ARTIFACT, "the sample skews enterprise")
    r = grade_mock(course, "00-data-audit", fdir)
    assert r["grade"] == 0.7 and not r["passed"], "cap must beat a passing base"
    assert "misconception_detected" in r["failure_flags"]
    assert r["weakest_concept"] == "limitation vs description"


def test_remedial_checkpoint_skips_tier2(tmp):
    course = compile_mock(tmp)
    with redirect_stdout(io.StringIO()):
        rid = compile_remedial(course, "00-data-audit",
                               "limitation vs description", LLM(mock=True))
    assert rid == "00-data-audit-r1"
    fdir = make_fixtures(tmp, {f"explain_{rid}": {
        "probes": [{"concept": "limitation vs description",
                    "verdict": "understood", "evidence": "boundary named",
                    "followup_question": ""}], "grade_cap": None}})
    submit(course, rid, None, "boundary: findings do not extend to SMB")
    r = grade_mock(course, rid, fdir)
    assert r["passed"] and "claim_audit" not in r, "no artifact -> no Tier 2"
    order = [m["id"] for m in yaml.safe_load(
        (course / "course.yaml").read_text())["milestones"]]
    assert order[0] == rid, "remedial must be served before its parent"


def _exec_course(tmp, script_body: str) -> Path:
    """Minimal executable-mode course reusing the 00-data-audit fixtures."""
    t = tmp / "exec"
    (t / "00-data-audit" / "starter").mkdir(parents=True)
    (t / "portfolio").mkdir()
    (t / "portfolio" / "state.yaml").write_text("entries: []\n")
    (t / "course.yaml").write_text(yaml.dump({
        "meta": {"title": "Exec", "domain": "technical", "version": "0.1.0",
                 "compiled_at": "2026-01-01T00:00:00Z", "topic_prompt": "x"},
        "learner": {"target_artifact": "benchmarked model"},
        "milestones": [{"id": "00-data-audit", "title": "Bench",
                        "estimated_hours": 2, "artifact_type": "code",
                        "checkpoint": "00-data-audit/checkpoint.yaml",
                        "misconception_target": "m", "sidequests": {},
                        "depends_on": []}]}))
    (t / "00-data-audit" / "checkpoint.yaml").write_text(yaml.dump({
        "milestone_id": "00-data-audit", "artifact_type": "code",
        "artifact_spec": "benchmark passes", "grader_type": "executable",
        "structural": {"required_files": ["reflection.md"]},
        "misconception_target": "m", "core_concepts": ["throughput"],
        "pass_threshold": 0.75,
        "rubric": {"scripts": ["starter/bench.py"]}}))
    (t / "00-data-audit" / "reflection.md").write_text("reflection")
    (t / "00-data-audit" / "starter" / "bench.py").write_text(script_body)
    return t


def test_rubric_script_pass(tmp):
    t = _exec_course(tmp, 'import json\nprint(json.dumps({"score": 0.9, "metrics": {}}))\n')
    r = grade_mock(t, "00-data-audit")
    assert r["passed"] and r["grade"] == 0.9
    assert r["tier_3_mode"] == "rubric_scripts"


def test_rubric_script_crash_fails_loudly(tmp):
    t = _exec_course(tmp, 'raise RuntimeError("model not found")\n')
    r = grade_mock(t, "00-data-audit")
    assert not r["passed"] and r["grade"] == 0.0
    assert any(f.startswith("script_error") for f in r["failure_flags"])
    assert "model not found" in r["feedback"], "stderr must reach the learner"


def test_rubric_script_garbage_output(tmp):
    t = _exec_course(tmp, 'print("not json at all")\n')
    r = grade_mock(t, "00-data-audit")
    assert not r["passed"]
    assert any(f.startswith("script_bad_output") for f in r["failure_flags"])


def test_rubric_script_score_out_of_range(tmp):
    t = _exec_course(tmp, 'import json\nprint(json.dumps({"score": 1.7}))\n')
    r = grade_mock(t, "00-data-audit")
    assert not r["passed"]
    assert any(f.startswith("script_bad_output") for f in r["failure_flags"])


# ------------------------------------------------------------- compiler seams

def test_intake_decline_refuses_to_compile(tmp):
    intake = json.loads((FIX / "intake.json").read_text())
    intake["viability"]["verdict"] = "decline"
    intake["viability"]["notes"] = "under 20% verifiable"
    fdir = make_fixtures(tmp, {"intake": intake})
    try:
        compile_mock(tmp, fdir)
    except SystemExit as e:
        assert "Declined" in str(e)
    else:
        raise AssertionError("decline verdict must stop the compile")


def test_check_dag_rejects_forward_dependency():
    ms = [{"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": []}]
    try:
        _check_dag(ms)
    except SystemExit as e:
        assert "later milestone" in str(e)
    else:
        raise AssertionError("forward dep must fail loudly")


def test_self_test_catches_corrupted_bundle(tmp):
    course = compile_mock(tmp)
    lesson = course / "00-data-audit" / "LESSON.md"
    _, body = okf.parse_doc(lesson)
    lesson.write_text(body)  # strip frontmatter
    inv = yaml.safe_load((course / "okf.yaml").read_text())
    inv["documents"].append({"path": "ghost.md", "type": "milestone",
                             "title": "Ghost"})
    (course / "okf.yaml").write_text(yaml.dump(inv))
    problems = self_test(course)
    assert any("frontmatter" in p for p in problems), problems
    assert any("ghost.md" in p for p in problems), problems


def test_okf_description_truncated():
    fm = okf.frontmatter("course", "T", "x" * 300)
    meta = yaml.safe_load(fm.split("---\n")[1])
    assert len(meta["description"]) == 120


def test_parse_json_strips_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


# ---------------------------------------------------------- path engine seams

def test_path_rules_and_idempotent_unlock(tmp):
    course = compile_mock(tmp)
    m0 = yaml.safe_load((course / "course.yaml").read_text())["milestones"][0]
    win = {"grade": 0.95, "attempt": 1, "passed": True, "failure_flags": [],
           "hours_actual": 1}
    ds = decide(course, m0, win)
    assert {"unlock_depth_sidequest"} == {d["action"] for d in ds}
    assert all(d["milestone_id"] == m0["id"] for d in ds)
    with redirect_stdout(io.StringIO()):
        actuate(course, ds)
        actuate(course, ds)  # second run must be a no-op
    assert event_types(course).count("sidequest.unlocked") == 1
    sq = yaml.safe_load((course / "sidequests" / "depth-question-redesign" /
                         "sidequest.yaml").read_text())
    assert sq["locked"] is False and sq["unlocked_at"]


def test_remedial_duplicate_guard(tmp):
    course = compile_mock(tmp)
    m0 = yaml.safe_load((course / "course.yaml").read_text())["milestones"][0]
    fail = {"grade": 0.4, "attempt": 1, "passed": False, "failure_flags": [],
            "hours_actual": 3, "weakest_concept": "limitation vs description"}
    ds = decide(course, m0, fail)
    assert any(d["action"] == "inject_remedial" for d in ds)
    with redirect_stdout(io.StringIO()):
        actuate(course, ds, llm=LLM(mock=True))
        actuate(course, ds, llm=LLM(mock=True))
    remedials = [m["id"] for m in yaml.safe_load(
        (course / "course.yaml").read_text())["milestones"]
        if "-r" in m["id"]]
    assert remedials == ["00-data-audit-r1"], "must not stack remedials"


def test_claim_flag_module_rule(tmp):
    course = compile_mock(tmp)
    m0 = yaml.safe_load((course / "course.yaml").read_text())["milestones"][0]
    noisy = {"grade": 0.8, "attempt": 1, "passed": True,
             "failure_flags": ["overclaiming", "missing_n",
                               "unsupported_causal", "false_precision"],
             "hours_actual": 3}
    ds = decide(course, m0, noisy)
    assert any(d["action"] == "inject_claim_audit_module" for d in ds)


# ------------------------------------------------------------- events + course

def test_course_completed_exactly_once(tmp):
    course = compile_mock(tmp)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(course, "00-data-audit")
    assert "course.completed" not in event_types(course)
    submit(course, "01-quant-skeleton", "42% of enterprise respondents (n=88).",
           "kept description separate from interpretation")
    grade_mock(course, "01-quant-skeleton")
    grade_mock(course, "01-quant-skeleton")  # re-grade must not re-emit
    assert event_types(course).count("course.completed") == 1


def test_events_schema(tmp):
    course = compile_mock(tmp)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(course, "00-data-audit")
    for line in (course / "events.jsonl").read_text().splitlines():
        e = json.loads(line)
        assert e["schema"] == 1
        for key in ("id", "ts", "type", "payload"):
            assert key in e, f"event missing {key}"


# -------------------------------------------------- journey (connected courses)

def _journey_home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    return home


def compile_into(home: Path, name: str) -> Path:
    out = home / "courses" / name
    with redirect_stdout(io.StringIO()):
        compile_course("Survey synthesis", {"weekly_hours": 5}, out,
                       LLM(mock=True))
    return out


def test_journey_next_and_submittable(tmp):
    home = _journey_home(tmp)
    course = compile_into(home, "survey")
    step = journey.next_steps(home)[0]
    assert step["status"] == "ready" and step["milestone_id"] == "00-data-audit"
    sub = journey.submittable(home)[0]
    assert sub["status"] == "awaiting_work" and "artifact.md" in sub["missing"]
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    sub = journey.submittable(home)[0]
    assert sub["status"] == "ready" and sub["missing"] == []
    grade_mock(course, "00-data-audit")
    assert journey.course_next(course)["milestone_id"] == "01-quant-skeleton"


def test_journey_knowledge_connects_courses(tmp):
    home = _journey_home(tmp)
    a = compile_into(home, "survey-a")
    b = compile_into(home, "survey-b")
    assert journey.prior_knowledge(home) == [], "nothing verified, nothing assumed"
    submit(a, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(a, "00-data-audit")
    know = journey.knowledge(home)
    concepts = {e["concept"] for e in know}
    assert {"non-response bias", "sampling frame"} <= concepts
    ev = next(e for e in know if e["concept"] == "sampling frame")["evidence"][0]
    assert ev["course"] == "survey-a" and ev["milestone_id"] == "00-data-audit"
    assert "sampling frame" in journey.prior_knowledge(home), \
        "verified concepts must feed the next compile"
    submit(b, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(b, "00-data-audit")
    e = next(e for e in journey.knowledge(home)
             if e["concept"] == "sampling frame")
    assert {ev["course"] for ev in e["evidence"]} == {"survey-a", "survey-b"}, \
        "one concept, evidence from both courses"


def test_journey_map_is_okf(tmp):
    home = _journey_home(tmp)
    course = compile_into(home, "survey")
    path = journey.emit_map(home)
    meta, _ = okf.parse_doc(path)
    assert meta["type"] == "journey" and meta["course_count"] == 1
    assert meta["concept_count"] == 0
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(course, "00-data-audit")
    journey.emit_map(home)
    meta, body = okf.parse_doc(home / "knowledge.md")
    assert meta["concept_count"] == 2
    assert "portfolio/claims/00-data-audit.md" in body, \
        "map must link concepts to their evidence"


def test_attach_connects_other_repos(tmp):
    """Cross-repo connection: a course living anywhere joins the journey
    (path -> symlink, git URL -> clone) and its knowledge counts."""
    import subprocess
    home = _journey_home(tmp)

    elsewhere = tmp / "elsewhere" / "survey"
    with redirect_stdout(io.StringIO()):
        compile_course("Survey synthesis", {"weekly_hours": 5}, elsewhere,
                       LLM(mock=True))
    linked = journey.attach(home, str(elsewhere))
    assert linked in journey.course_dirs(home)

    repo = tmp / "repo-course"
    with redirect_stdout(io.StringIO()):
        compile_course("Survey synthesis", {"weekly_hours": 5}, repo,
                       LLM(mock=True))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "course"], cwd=repo, check=True)
    cloned = journey.attach(home, f"file://{repo}")
    assert (cloned / "course.yaml").exists() and (cloned / ".git").exists()

    submit(linked, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(linked, "00-data-audit")
    submit(cloned, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(cloned, "00-data-audit")
    e = next(e for e in journey.knowledge(home)
             if e["concept"] == "sampling frame")
    assert len({ev["course"] for ev in e["evidence"]}) == 2, \
        "knowledge must connect across attached repos"

    try:
        journey.attach(home, str(tmp))  # not a course bundle
    except SystemExit as e:
        assert "course.yaml" in str(e)
    else:
        raise AssertionError("attaching a non-course must fail loudly")


# -------------------------------------------------------- journey tool surface

def test_tools_walk_the_loop(tmp):
    home = _journey_home(tmp)
    tools = JourneyTools(home, mock=True)
    assert "start_course" in tools.call("journey", {}), \
        "empty journey must point at start_course"
    with redirect_stdout(io.StringIO()):
        out = tools.call("start_course", {"topic": "Survey synthesis"})
    assert "survey-synthesis" in out
    lesson = tools.call("get_lesson", {"course": "survey-synthesis",
                                       "milestone_id": "00-data-audit"})
    assert lesson.strip()
    assert "milestone.started" in event_types(home / "courses" /
                                              "survey-synthesis")
    with redirect_stdout(io.StringIO()):
        fb = tools.call("submit_work", {
            "course": "survey-synthesis", "milestone_id": "00-data-audit",
            "artifact": STRONG_ARTIFACT, "reflection": STRONG_REFLECTION})
    assert "PASSED" in fb and "Path decisions" in fb
    assert "non-response bias" in tools.call("knowledge_map", {})
    assert (home / "knowledge.md").exists(), "submit_work refreshes the map"
    assert "passed" in tools.call("progress", {"course": "survey-synthesis"})


def test_tools_refuse_path_traversal(tmp):
    tools = JourneyTools(_journey_home(tmp), mock=True)
    for args in ({"course": "../evil", "milestone_id": "x"},
                 {"course": "a/b", "milestone_id": "x"},
                 {"course": "ghost", "milestone_id": "x"}):
        try:
            tools.call("get_lesson", args)
        except ToolError:
            continue
        raise AssertionError(f"must refuse {args}")


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


class _StubModel:
    """Stands in for LLM.chat; streams text through on_text like the real
    client so the harness's live path is what gets exercised."""

    def __init__(self, replies):
        self.replies = replies

    def chat(self, system, messages, tools, on_text=None, **kw):
        resp = self.replies.pop(0)
        if on_text is not None:
            for b in resp.content:
                if b.type == "text":
                    on_text(b.text)
        return resp


def _stub_agent(tmp, replies):
    from sylabis.agent import Agent
    from sylabis.console import Console
    return Agent(_journey_home(tmp), llm=_StubModel(replies),
                 console=Console(enabled=False))


def test_agent_tool_loop(tmp):
    """The harness plumbing: tool_use -> run tool -> tool_result -> text.
    The model is a stub; the tools are real (mock LLM underneath)."""
    replies = [
        _Resp([_Block(type="tool_use", name="journey", input={}, id="t1")],
              "tool_use"),
        _Resp([_Block(type="tool_use", name="get_lesson",
                      input={"course": "ghost", "milestone_id": "x"},
                      id="t2")], "tool_use"),
        _Resp([_Block(type="text", text="Let's begin.")], "end_turn"),
    ]
    agent = _stub_agent(tmp, replies)
    messages = [{"role": "user", "content": "hi"}]
    reply = agent.turn(messages)
    assert reply == "Let's begin."
    assert not agent.llm.replies and len(messages) == 6, "full turn transcript"
    ok = messages[2]["content"][0]
    assert ok["type"] == "tool_result" and not ok["is_error"]
    assert "start_course" in ok["content"], "empty journey orients the model"
    bad = messages[4]["content"][0]
    assert bad["is_error"] and "ghost" in bad["content"], \
        "ToolError becomes a recoverable result, not a crash"


def test_agent_interrupt_rolls_back_turn(tmp):
    """Ctrl-C mid-turn must leave the transcript exactly as it was — a
    dangling tool_use without its result would poison every later call."""
    class _Boom:
        def chat(self, *a, **kw):
            raise KeyboardInterrupt

    replies = [
        _Resp([_Block(type="tool_use", name="journey", input={}, id="t1")],
              "tool_use"),
    ]
    agent = _stub_agent(tmp, replies)
    messages = [{"role": "user", "content": "hi"}]
    # first round returns a tool_use; the second model call gets interrupted
    real = agent.llm

    class _TwoPhase:
        def chat(self, *a, **kw):
            if real.replies:
                return real.chat(*a, **kw)
            raise KeyboardInterrupt
    agent.llm = _TwoPhase()
    reply = agent.turn(messages)
    assert reply == "" and messages == [{"role": "user", "content": "hi"}], \
        "interrupted turn must be rolled back whole"


def test_console_trace_previews():
    from sylabis.console import Console, preview_args, preview_result
    s = preview_args({"artifact": "x" * 500, "course": "survey"})
    assert "(500 chars)" in s and "x" * 60 not in s, \
        "long values collapse to a length note"
    assert 'course: "survey"' in s
    lines = preview_result("\n".join(f"line {i}" for i in range(12)))
    assert lines[0] == "line 0" and lines[-1] == "… +8 more lines"
    assert preview_result("") == ["(empty)"]
    quiet = Console(enabled=False)
    quiet.tool_call("journey", {})  # every method must be a silent no-op
    quiet.text_delta("x")
    quiet.spinner("thinking")
    quiet.stop_spinner()


def test_mcp_serves_both_scopes(tmp):
    home = _journey_home(tmp)
    jnames = {t["name"] for t in
              MCPServer(None, mock=True, home_dir=home)
              ._dispatch("tools/list", {})["tools"]}
    assert {"journey", "start_course", "submit_work"} <= jnames
    course = compile_mock(tmp)
    cnames = {t["name"] for t in MCPServer(course, mock=True)
              ._dispatch("tools/list", {})["tools"]}
    assert {"course_overview", "submit_artifact"} <= cnames, \
        "per-course scope must keep working for existing bundles"


# ------------------------------------------------------------ web interface

def test_md_to_html_subset():
    from sylabis.web import md_to_html
    out = md_to_html("# T\n\n- a\n- **b**\n\n```\n<x>\n```\n\n"
                     "see [doc](knowledge/index.md) `c`")
    assert "<h1>T</h1>" in out and "<li><strong>b</strong></li>" in out
    assert "&lt;x&gt;" in out, "code blocks must be escaped"
    assert '<a href="knowledge/index.md">doc</a>' in out
    assert "<code>c</code>" in out
    assert "<script" not in md_to_html("<script>alert(1)</script>"), \
        "raw html must never pass through"


def test_web_first_run_compiles(tmp):
    """The Reading Room first-run: an empty journey invites a topic, and
    POST /learn compiles a course into the journey."""
    import http.client
    import threading
    from sylabis.web import make_server

    home = _journey_home(tmp)
    server = make_server(home, port=0, mock=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def req(method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", port)
        conn.request(method, path, body=body,
                     headers={"Content-Type": "application/json"}
                     if body else {})
        resp = conn.getresponse()
        return resp.status, resp.read().decode()

    try:
        status, page = req("GET", "/")
        assert status == 200 and "What do you want to" in page
        status, out = req("POST", "/learn",
                          json.dumps({"topic": "Survey synthesis"}))
        assert status == 200 and json.loads(out) == {"ok": True}
        assert journey.course_dirs(home), "compile must land in the journey"
        status, out = req("POST", "/learn", json.dumps({"topic": "  "}))
        assert status == 400, "an empty topic is refused, not compiled"
        status, page = req("GET", "/")
        assert "Open the lesson" in page, "journey replaces first-run"
    finally:
        server.shutdown()


def test_web_serves_the_loop(tmp):
    """The standard interface: dashboard, lesson, submit form -> graded
    feedback, knowledge map with the SVG graph — over real HTTP."""
    import http.client
    import threading
    from urllib.parse import urlencode
    from sylabis.web import make_server

    home = _journey_home(tmp)
    compile_into(home, "survey")
    server = make_server(home, port=0, mock=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def req(method, path, body=None, ctype="application/x-www-form-urlencoded"):
        conn = http.client.HTTPConnection("127.0.0.1", port)
        headers = {"Content-Type": ctype} if body else {}
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()

    try:
        status, page = req("GET", "/")
        assert status == 200 and "Survey Synthesis" in page
        assert "Open the lesson" in page, "dashboard leads with ONE next action"
        assert "Ask Sy" in page, "Sy waits behind a tab on every page"

        status, page = req("GET", "/course/survey/lesson/00-data-audit")
        assert status == 200 and "Submit your work" in page

        status, page = req("POST", "/course/survey/submit/00-data-audit",
                           urlencode({"artifact": STRONG_ARTIFACT,
                                      "reflection": STRONG_REFLECTION,
                                      "hours": "2"}))
        assert status == 200 and "Passed" in page

        status, page = req("GET", "/knowledge")
        assert status == 200 and "<svg" in page
        assert "sampling frame" in page, "verified concepts reach the map"

        status, _ = req("GET", "/course/../../etc")
        assert status == 404
        status, _ = req("GET", "/course/survey/doc?p=../../secrets.md")
        assert status == 404, "doc route must refuse traversal"
        status, page = req("GET", "/course/survey/doc?p=knowledge/index.md")
        assert status == 200 and "Knowledge base" in page
    finally:
        server.shutdown()


# ------------------------------------------------------------------ verify.py

def test_resolve_locator_forms():
    assert resolve_locator("arXiv:1503.02531") == ("1503.02531", "arxiv")
    assert resolve_locator("1503.02531v2") == ("1503.02531v2", "arxiv")
    assert resolve_locator("doi:10.1191/1478088706qp063oa") == (
        "https://doi.org/10.1191/1478088706qp063oa", "doi")
    assert resolve_locator("https://doi.org/10.5555/x") == (
        "https://doi.org/10.5555/x", "doi")
    assert resolve_locator("https://docs.python.org") == (
        "https://docs.python.org", "http")
    assert resolve_locator("search: fowler survey methods")[1] == "search"
    assert resolve_locator("Fowler 5th edition")[1] == "opaque"
    assert resolve_locator("")[1] == "opaque"


def test_verify_disabled_never_touches_network():
    sources = [{"locator": "doi:10.1/x"}, {"locator": "arXiv:1503.02531"},
               {"locator": "search: foo"}, {"locator": "gibberish"}]
    summary = verify_sources(sources, enabled=False)
    assert summary == {"verified": 0, "unverified": 0,
                       "flagged_search": 1, "skipped": 3}
    assert sources[0]["verified"] is None
    assert sources[2]["verified"] is False, "search locators are flagged"


# --------------------------------------------------------------------- runner

TESTS = [v for k, v in sorted(globals().items())
         if k.startswith(("case_", "test_")) and callable(v)
         and getattr(v, "__module__", __name__) == __name__]

# Auto-collect from sibling tests/test_*.py modules so workstreams add
# suites as new files instead of all editing this one. Same conventions:
# top-level case_*/test_* functions, optional (tmp: Path) arg.
import importlib  # noqa: E402

for _stem in sorted(p.stem for p in Path(__file__).parent.glob("test_*.py")):
    _mod = importlib.import_module(f"tests.{_stem}")
    TESTS.extend(v for k, v in sorted(vars(_mod).items())
                 if k.startswith(("case_", "test_")) and callable(v)
                 and getattr(v, "__module__", None) == _mod.__name__)


def main() -> int:
    failed = []
    for fn in TESTS:
        try:
            if fn.__code__.co_argcount:
                with tempfile.TemporaryDirectory() as td:
                    fn(Path(td))
            else:
                fn()
            print(f"PASS  {fn.__name__}")
        except BaseException as e:
            failed.append(fn.__name__)
            print(f"FAIL  {fn.__name__}: {e.__class__.__name__}: {e}")
    print(f"\n{len(TESTS) - len(failed)}/{len(TESTS)} passed"
          + (f" — FAILURES: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
