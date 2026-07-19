"""
WS3a attach-hardening + WS4.3 grader-calibration suite. Adversarial and
fully offline: hostile rubric script paths are refused before anything
executes, executed scripts get a scrubbed environment (never the API
key), attach copies instead of symlinking and records provenance,
pre-existing grade records in attached bundles never seed prior
knowledge, the first grade of an attached bundle with rubric scripts is
consent-gated, rubric-less executable checkpoints yield 'unscored' (not
a fabricated 0.85), and the claim-audit fallback can no longer let a
~38%-claims-passing artifact clear 0.75.

Sandbox tests never require bwrap/nsjail installed: backend selection is
driven by injected which/probe fakes, and everything that actually
executes runs under the (warned) fallback runner.
"""
import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from sylabis import cli, journey, sandbox
from sylabis.errors import AttachError, SandboxError
from sylabis.compiler import compile_course
from sylabis.grader import grade, path_engine_view
from sylabis.llm import LLM
from sylabis.sandbox import ConsentRequired, SandboxRunner
from sylabis.tools import JourneyTools, ToolError

FIX = Path(__file__).parent.parent / "fixtures"

STRONG_ARTIFACT = ("61% of respondents (n=140) reported satisfaction. "
                   "Findings cannot be generalized to SMB customers.")
STRONG_REFLECTION = ("I wanted the data to support more than it can; "
                     "the skew is a generalization boundary.")


# -------------------------------------------------------------- helpers

def make_fixtures(tmp: Path, overlays: dict) -> Path:
    fdir = tmp / "fixtures"
    shutil.copytree(FIX, fdir)
    for stage, content in overlays.items():
        (fdir / f"{stage}.json").write_text(json.dumps(content))
    return fdir


def compile_mock(out: Path, fdir: Path | None = None) -> Path:
    with redirect_stdout(io.StringIO()):
        compile_course("Survey synthesis", {"weekly_hours": 5}, out,
                       LLM(mock=True, fixtures_dir=fdir or FIX))
    return out


def grade_mock(course: Path, mid: str, fdir: Path | None = None, **kw) -> dict:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return grade(course, mid, LLM(mock=True, fixtures_dir=fdir or FIX),
                     **kw)


def submit(course: Path, mid: str, artifact: str | None,
           reflection: str | None) -> None:
    if artifact is not None:
        (course / mid / "artifact.md").write_text(artifact)
    if reflection is not None:
        (course / mid / "reflection.md").write_text(reflection)


def exec_course(t: Path, scripts: list[str] | None,
                script_body: str | None = None) -> Path:
    """Minimal executable-mode course (reuses 00-data-audit fixtures).
    scripts=None omits the rubric block entirely (the unscored case)."""
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
    checkpoint = {
        "milestone_id": "00-data-audit", "artifact_type": "code",
        "artifact_spec": "benchmark passes", "grader_type": "executable",
        "structural": {"required_files": ["reflection.md"]},
        "misconception_target": "m", "core_concepts": ["throughput"],
        "pass_threshold": 0.75}
    if scripts is not None:
        checkpoint["rubric"] = {"scripts": scripts}
    (t / "00-data-audit" / "checkpoint.yaml").write_text(yaml.dump(checkpoint))
    (t / "00-data-audit" / "reflection.md").write_text("reflection")
    if script_body is not None:
        (t / "00-data-audit" / "starter" / "bench.py").write_text(script_body)
    return t


def _journey_home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    return home


# ------------------------------------------- script path containment (3a.1b)

def case_adversarial_rubric_paths_refused(tmp):
    """'../'-escaping, absolute, and nonexistent rubric script paths are
    refused with a typed error BEFORE anything executes."""
    outside = tmp / "outside.py"
    outside.write_text("import json; print(json.dumps({'score': 1.0}))\n")

    for i, script in enumerate((
            f"../../{outside.name}",   # escapes the course bundle
            str(outside),              # absolute path
            "starter/ghost.py")):      # nonexistent
        cdir = tmp / f"c-{i}"
        exec_course(cdir, [script])
        try:
            grade_mock(cdir, "00-data-audit")
        except SandboxError as e:
            assert "refused" in str(e) or "does not exist" in str(e)
        else:
            raise AssertionError(f"script path {script!r} must be refused")
        assert not (cdir / "00-data-audit" / "grade.yaml").exists(), \
            "a refused script must not produce a grade record"


def case_symlinked_script_escape_refused(tmp):
    """A relative script path that is a symlink out of the bundle resolves
    outside and is refused — resolve() must run before containment."""
    outside = tmp / "outside.py"
    outside.write_text("import json; print(json.dumps({'score': 1.0}))\n")
    cdir = exec_course(tmp / "course", ["starter/link.py"])
    (cdir / "00-data-audit" / "starter" / "link.py").symlink_to(outside)
    try:
        grade_mock(cdir, "00-data-audit")
    except SandboxError as e:
        assert "escapes" in str(e)
    else:
        raise AssertionError("symlinked escape must be refused")


# --------------------------------------------------- env scrub + sandbox (3a.1)

def case_rubric_script_cannot_read_api_key(tmp):
    """A really-executed script under the fallback runner sees only the
    allowlisted environment — never ANTHROPIC_API_KEY."""
    prior = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-secret"
    os.environ["SYLABIS_TEST_SENTINEL"] = "must-not-leak"
    try:
        # Unit level: the runner's env, observed by a real child process.
        err = io.StringIO()
        runner = SandboxRunner(origin="unit-test-bundle", unsandboxed=True)
        with redirect_stderr(err):
            proc = runner.run(
                [sys.executable, "-c",
                 "import json, os; print(json.dumps(dict(os.environ)))"],
                cwd=tmp, timeout=60)
        child_env = json.loads(proc.stdout.strip().splitlines()[-1])
        assert "ANTHROPIC_API_KEY" not in child_env, "the key must be scrubbed"
        assert "SYLABIS_TEST_SENTINEL" not in child_env, \
            "nothing outside the allowlist may pass"
        # The interpreter's own locale coercion (PEP 538) may add LC_CTYPE
        # inside the child; everything WE passed must be allowlisted.
        assert set(runner.env) <= set(sandbox.ENV_ALLOWLIST)
        assert "unit-test-bundle" in err.getvalue(), \
            "the fallback warning must name the bundle's origin"

        # Grade level: the script scores 1.0 only if the key is absent.
        cdir = exec_course(tmp / "course", ["starter/bench.py"],
                           'import json, os\n'
                           'print(json.dumps({"score": 1.0 if '
                           '"ANTHROPIC_API_KEY" not in os.environ else 0.0,'
                           ' "metrics": {}}))\n')
        r = grade_mock(cdir, "00-data-audit", unsandboxed=True)
        assert r["grade"] == 1.0 and r["passed"], \
            "the grading subprocess must not see the API key"
    finally:
        os.environ.pop("SYLABIS_TEST_SENTINEL", None)
        if prior is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = prior


def test_sandbox_command_construction(tmp):
    """Backend selection and containment flags, no bwrap install needed."""
    os.environ.setdefault("HOME", "/root")
    argv = [sys.executable, "bench.py"]

    b = SandboxRunner(origin="o", which=lambda t: "/usr/bin/" + t
                      if t == "bwrap" else None, probe=lambda t: True)
    assert b.backend == "bwrap"
    cmd = b.command(argv, tmp)
    for flag in ("--unshare-all", "--ro-bind", "--clearenv", "--tmpfs",
                 "--die-with-parent"):
        assert flag in cmd, f"bwrap command missing {flag}: {cmd}"
    assert cmd[-len(argv):] == argv and "--" in cmd
    assert "ANTHROPIC_API_KEY" not in " ".join(cmd)

    n = SandboxRunner(origin="o", which=lambda t: "/usr/bin/" + t
                      if t == "nsjail" else None, probe=lambda t: True)
    assert n.backend == "nsjail"
    cmd = n.command(argv, tmp)
    for flag in ("--rlimit_cpu", "--rlimit_as", "--rlimit_nproc", "--chroot"):
        assert flag in cmd, f"nsjail command missing {flag}: {cmd}"
    assert "ANTHROPIC_API_KEY" not in " ".join(cmd)

    # A binary on PATH that fails its probe must NOT be selected.
    broken = SandboxRunner(origin="o", which=lambda t: "/usr/bin/" + t,
                           probe=lambda t: False)
    assert broken.backend == "none"
    assert broken.command(argv, tmp) == argv

    # unsandboxed=True forces the fallback even when a sandbox exists,
    # and warns exactly once.
    u = SandboxRunner(origin="the-origin", unsandboxed=True,
                      which=lambda t: "/usr/bin/" + t, probe=lambda t: True)
    assert u.backend == "none"
    err = io.StringIO()
    with redirect_stderr(err):
        u._warn_if_uncontained()
        u._warn_if_uncontained()
    assert err.getvalue().count("the-origin") == 1, "warn once, loudly"


# ------------------------------------------------------ copy-on-attach (3a.2)

def case_attach_copies_and_original_stays_untouched(tmp):
    home = _journey_home(tmp)
    original = compile_mock(tmp / "elsewhere" / "survey")
    before_events = (original / "events.jsonl").read_bytes()

    dest = journey.attach(home, str(original))
    assert not dest.is_symlink(), "attach must copy, never symlink"
    info = journey.attach_info(dest)
    assert info["source"] == str(original) and info["commit"] is None
    assert info["attached_at"] and info["preexisting_grades"] == {}

    submit(dest, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    r = grade_mock(dest, "00-data-audit")
    assert r["passed"]
    assert not (original / "00-data-audit" / "grade.yaml").exists(), \
        "grading the attached copy must not touch the original"
    assert not (original / "00-data-audit" / "artifact.md").exists()
    assert (original / "events.jsonl").read_bytes() == before_events, \
        "the original's event log must be untouched"
    assert journey.attach_info(original) is None, \
        "provenance is stamped into the copy, not the source"


def case_git_attach_pins_commit_sha(tmp):
    home = _journey_home(tmp)
    repo = compile_mock(tmp / "repo-course")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "course"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()
    dest = journey.attach(home, f"file://{repo}")
    info = journey.attach_info(dest)
    assert info["commit"] == sha, "clone must record the pinned commit SHA"
    assert info["source"] == f"file://{repo}"

    try:
        journey.attach(home, "file:///nonexistent/nowhere.git")
    except AttachError:
        pass
    else:
        raise AssertionError("failed clone must raise AttachError")


# ------------------------------------------ origin tagging + exclusion (3a.3)

def case_tampered_attached_grade_never_seeds_prior_knowledge(tmp):
    home = _journey_home(tmp)
    original = compile_mock(tmp / "elsewhere" / "survey")
    # The attack: a hand-edited passing grade shipped inside the bundle.
    (original / "00-data-audit" / "grade.yaml").write_text(yaml.dump({
        "milestone_id": "00-data-audit", "attempt": 1, "grade": 0.99,
        "passed": True, "graded_at": "2026-01-01T00:00:00Z"}))

    dest = journey.attach(home, str(original))
    know = journey.knowledge(home)
    assert know, "the forged grade still shows in the evidence trail"
    for e in know:
        for ev in e["evidence"]:
            assert ev["origin"].startswith("attached:"), ev
            assert ev.get("preexisting") is True, \
                "a grade that shipped with the bundle must be fingerprinted"
    assert journey.prior_knowledge(home) == [], \
        "pre-existing attached grades must never seed the next compile"

    # Doing the work yourself rewrites grade.yaml — then it counts fully.
    submit(dest, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    r = grade_mock(dest, "00-data-audit")
    assert r["passed"]
    assert "sampling frame" in journey.prior_knowledge(home), \
        "work done after attach must count"
    ev = next(e for e in journey.knowledge(home)
              if e["concept"] == "sampling frame")["evidence"][0]
    assert ev["origin"].startswith("attached:") and not ev.get("preexisting")


def case_local_courses_tagged_local(tmp):
    home = _journey_home(tmp)
    course = compile_mock(home / "courses" / "survey")
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(course, "00-data-audit")
    know = journey.knowledge(home)
    assert all(ev["origin"] == "local"
               for e in know for ev in e["evidence"])
    assert "sampling frame" in journey.prior_knowledge(home)


# ------------------------------------------------- first-grade consent (3a.4)

def case_consent_gate_blocks_scripts_until_recorded(tmp):
    home = _journey_home(tmp)
    original = exec_course(
        tmp / "elsewhere", ["starter/bench.py"],
        'import json, pathlib\n'
        'pathlib.Path("ran.txt").write_text("x")\n'
        'print(json.dumps({"score": 0.9, "metrics": {}}))\n')
    dest = journey.attach(home, str(original))

    try:
        grade_mock(dest, "00-data-audit")
    except ConsentRequired as e:
        assert "starter/bench.py" in e.scripts
        assert str(original) in e.source
    else:
        raise AssertionError("first grade of an attached bundle with "
                             "scripts must require consent")
    assert not (dest / "00-data-audit" / "ran.txt").exists(), \
        "nothing may execute before consent"
    assert not (dest / "00-data-audit" / "grade.yaml").exists()

    marker = sandbox.record_trust(dest, str(original), ["starter/bench.py"])
    assert marker.name == ".sylabis-trust" and sandbox.trusted(dest)
    r = grade_mock(dest, "00-data-audit", unsandboxed=True)
    assert r["passed"] and r["grade"] == 0.9
    assert (dest / "00-data-audit" / "ran.txt").exists(), \
        "after recorded trust the script runs"

    # Local (non-attached) bundles are the learner's own compile: no gate.
    local = exec_course(
        tmp / "local",
        ["starter/bench.py"],
        'import json\nprint(json.dumps({"score": 0.8, "metrics": {}}))\n')
    r = grade_mock(local, "00-data-audit", unsandboxed=True)
    assert r["passed"]


def case_consent_is_typed_error_on_tool_surface(tmp):
    """Non-interactive surfaces (tools → web/MCP) surface the consent
    requirement as a recoverable typed error carrying the script list —
    never a hang, never a crash, never an execution."""
    home = _journey_home(tmp)
    original = exec_course(
        tmp / "elsewhere", ["starter/bench.py"],
        'import json\nprint(json.dumps({"score": 0.9, "metrics": {}}))\n')
    dest = journey.attach(home, str(original))

    tools = JourneyTools(home, mock=True)
    try:
        with redirect_stdout(io.StringIO()):
            tools.call("submit_work", {
                "course": dest.name, "milestone_id": "00-data-audit",
                "artifact": "a", "reflection": "r"})
    except ToolError as e:
        assert "starter/bench.py" in str(e), \
            "the error must carry the script list"
        assert "nothing was executed" in str(e)
    else:
        raise AssertionError("consent must surface as ToolError")
    assert not (dest / "00-data-audit" / "grade.yaml").exists()


def case_cli_submit_records_consent_once(tmp):
    home = _journey_home(tmp)
    original = exec_course(
        tmp / "elsewhere", ["starter/bench.py"],
        'import json\nprint(json.dumps({"score": 0.9, "metrics": {}}))\n')
    dest = journey.attach(home, str(original))

    real_confirm = cli._confirm_consent
    cli._confirm_consent = lambda e: True
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                cli.main(["submit", "--mock", "--unsandboxed",
                          "--home", str(home)])
            except SystemExit as e:
                assert e.code == 0, f"submit should pass, exited {e.code}"
    finally:
        cli._confirm_consent = real_confirm
    assert sandbox.trusted(dest), "the decision must be recorded per bundle"
    r = yaml.safe_load((dest / "00-data-audit" / "grade.yaml").read_text())
    assert r["passed"] and r["grade"] == 0.9

    # Declining leaves the typed error to propagate — nothing executed.
    original2 = exec_course(
        tmp / "elsewhere2", ["starter/bench.py"],
        'import json\nprint(json.dumps({"score": 0.9, "metrics": {}}))\n')
    dest2 = journey.attach(home, str(original2))
    cli._confirm_consent = lambda e: False
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                cli.main(["submit", dest2.name, "--mock", "--home", str(home)])
            except SystemExit as e:
                assert "rubric scripts" in str(e.code), \
                    "declined consent renders the typed error"
            else:
                raise AssertionError("declined consent must not grade")
    finally:
        cli._confirm_consent = real_confirm
    assert not sandbox.trusted(dest2)
    assert not (dest2 / "00-data-audit" / "grade.yaml").exists()


# ------------------------------------------------- WS4.3 calibration fixes

def case_rubric_less_executable_is_unscored_not_085(tmp):
    cdir = exec_course(tmp / "course", scripts=None)  # no rubric block
    r = grade_mock(cdir, "00-data-audit")
    assert r["tier_3_mode"] == "unscored" and r.get("unscored") is True
    assert r["grade"] is None, "never fabricate 0.85"
    assert r["passed"], "pass/fail comes from Tiers 1-2 + explain-back"
    assert "unscored" in r["feedback"]
    gy = yaml.safe_load((cdir / "00-data-audit" / "grade.yaml").read_text())
    assert "grade" not in gy and gy["unscored"] is True
    assert gy["tier_3_mode"] == "unscored" and gy["passed"] is True
    assert not (cdir / "portfolio" / "claims" / "00-data-audit.md").exists(), \
        "an unscored pass mints no scored portfolio claim"

    # The path engine sees neutral numbers, never None.
    view = path_engine_view(r)
    assert 0.6 <= view["grade"] < 0.9, "no unlock, no remediation on pass"
    assert path_engine_view({"grade": None, "passed": False})["grade"] == 0.0
    assert path_engine_view({"grade": 0.9, "passed": True})["grade"] == 0.9


def case_unscored_still_blocked_by_explain_back_cap(tmp):
    explain = {"probes": [
        {"concept": "throughput", "verdict": "misconception",
         "evidence": "confuses throughput with latency",
         "followup_question": "What does the benchmark actually measure?"}],
        "grade_cap": 0.5}
    fdir = make_fixtures(tmp, {"explain_00-data-audit": explain})
    cdir = exec_course(tmp / "course", scripts=None)
    r = grade_mock(cdir, "00-data-audit", fdir)
    assert r["tier_3_mode"] == "unscored" and r["grade"] is None
    assert not r["passed"], "a detected misconception beats an unscored pass"
    assert "misconception_detected" in r["failure_flags"]
    assert r["weakest_concept"] == "throughput"


def case_38pct_claim_audit_cannot_clear_075(tmp):
    """The old floor (0.6 + 0.4*ratio) gave 3/8 passing claims a 0.75.
    The re-derived base equals the pass ratio itself."""
    claims = ([{"text": f"claim {i}", "type": "descriptive",
                "evidence": "ok", "result": "pass", "flag": None,
                "feedback": ""} for i in range(3)]
              + [{"text": f"weak claim {i}", "type": "descriptive",
                  "evidence": "missing", "result": "fail",
                  "flag": "missing_n", "feedback": "add n"}
                 for i in range(5)])
    audit = {"claims": claims,
             "summary": {"total": 8, "passed": 3, "failed": 5,
                         "flags": [], "blocking": False}}
    fdir = make_fixtures(tmp, {"audit_00-data-audit": audit})
    course = compile_mock(tmp / "course", fdir)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    r = grade_mock(course, "00-data-audit", fdir)
    assert r["tier_3_mode"] == "claim_audit_derived"
    assert r["grade"] < 0.75 and not r["passed"], \
        f"38% claims passing must not clear the bar (got {r['grade']})"
    assert abs(r["grade"] - 3 / 8) < 1e-9, "base is the pass ratio itself"


def case_full_pass_ratio_still_passes(tmp):
    """Recalibration must not fail genuinely strong work: an all-claims-
    passing artifact keeps a passing claim-audit-derived grade."""
    course = compile_mock(tmp / "course")
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    r = grade_mock(course, "00-data-audit")
    assert r["tier_3_mode"] == "claim_audit_derived"
    assert r["grade"] == 1.0 and r["passed"]


# ------------------------------------------- Wave-1 grader integration

def case_grade_logs_usage_and_stamps_provenance(tmp):
    from sylabis import __version__
    course = compile_mock(tmp / "course")
    llm = LLM(mock=True)
    assert llm.usage_dir is None
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    with redirect_stdout(io.StringIO()):
        r = grade(course, "00-data-audit", llm)
    assert llm.usage_dir is None, "usage_dir must be restored after grading"
    usage = [json.loads(line) for line
             in (course / "events.jsonl").read_text().splitlines()
             if json.loads(line)["type"] == "llm.usage"]
    stages = {u["payload"]["stage"] for u in usage}
    assert {"audit_00-data-audit", "explain_00-data-audit"} <= stages, \
        "grading must log llm.usage per stage"

    prov = r["provenance"]
    assert set(prov) == {"audit", "explain"}, "tier3 didn't run here"
    for family, stamp in prov.items():
        assert stamp["sylabis_version"] == __version__
        assert stamp["model"] and stamp["prompt_version"] != "unversioned"
    gy = yaml.safe_load((course / "00-data-audit" / "grade.yaml").read_text())
    assert gy["provenance"] == prov, "provenance must reach grade.yaml"


def case_malformed_audit_fixture_is_schema_error_not_keyerror(tmp):
    """WS1a integration: a fixture missing summary keys now raises the
    typed SchemaError after one repair round — never a bare KeyError."""
    from sylabis.errors import SchemaError
    fdir = make_fixtures(tmp, {"audit_00-data-audit": {"claims": []}})
    course = compile_mock(tmp / "course", fdir)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    try:
        grade_mock(course, "00-data-audit", fdir)
    except SchemaError as e:
        assert "summary" in str(e)
    else:
        raise AssertionError("malformed audit output must be a SchemaError")
