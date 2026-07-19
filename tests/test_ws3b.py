"""
WS3b suite: `sy publish` — the privacy scrubber.

Publishes a FULLY-USED mock course (compiled with distinctive learner
markers, graded through the real mock grade path, remediated, sidequest
unlocked, portfolio claims emitted) and then attacks the output: the
adversarial grep asserts zero occurrences of the learner's hardware
string, topic prompt, artifact sentences, and reflection sentences
anywhere in the template tree. Also covers: re-attach + startability of
the template, the planted-secret abort, the PII gate classes, the
mandatory license, journey-home dotfile isolation, and okf.yaml's
tamper-evident per-file hashes.

Fully offline and deterministic: every model response is a fixture.
"""
import io
import json
import shutil
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from sylabis import journey, okf
from sylabis.compiler import compile_course, compile_remedial, self_test
from sylabis.errors import PublishError
from sylabis.grader import grade
from sylabis.llm import LLM
from sylabis.path_engine import actuate, decide
from sylabis.publish import pii_problems, publish_course, scan_secrets

FIX = Path(__file__).parent.parent / "fixtures"

# Distinctive learner markers — none may survive into a template.
TOPIC = "TOPICPROMPT9 quantize a llama on my attic rig"
HW = "HWSTRING7-rtx-6000-in-the-attic"
ARTIFACT = ("ARTSENT3 61% of respondents (n=140) reported satisfaction. "
            "Findings cannot be generalized to SMB customers.")
REFLECTION = ("REFLSENT5 I wanted the data to support more than it can; "
              "the skew is a generalization boundary.")


def _fixtures_with_learner_markers(tmp: Path) -> Path:
    """Overlay the intake fixture so the compiled course.yaml actually
    embeds the marker hardware/hours (the learner block the scrub must
    remove)."""
    fdir = tmp / "fixtures"
    shutil.copytree(FIX, fdir)
    intake = json.loads((FIX / "intake.json").read_text())
    intake["constraints"] = {"weekly_hours": 7, "hardware": HW}
    (fdir / "intake.json").write_text(json.dumps(intake))
    return fdir


def _used_course(tmp: Path, out: Path | None = None) -> Path:
    """Compile + fully use a mock course: remedial injected and passed,
    milestone 00 graded to a pass (portfolio claim), sidequest unlocked."""
    fdir = _fixtures_with_learner_markers(tmp)
    out = out or tmp / "course"
    llm = LLM(mock=True, fixtures_dir=fdir)
    with redirect_stdout(io.StringIO()):
        compile_course(TOPIC, {"weekly_hours": 7, "hardware": HW,
                               "prior_knowledge": ["spreadsheets"]},
                       out, llm)
        rid = compile_remedial(out, "00-data-audit",
                               "limitation vs description", llm)
        (out / rid / "reflection.md").write_text(REFLECTION)
        r_rem = grade(out, rid, llm)
        assert r_rem["passed"], "remedial must pass in this scenario"
        (out / "00-data-audit" / "artifact.md").write_text(ARTIFACT)
        (out / "00-data-audit" / "reflection.md").write_text(REFLECTION)
        r00 = grade(out, "00-data-audit", llm)
        assert r00["passed"], "milestone 00 must pass in this scenario"
        m0 = next(m for m in yaml.safe_load(
            (out / "course.yaml").read_text())["milestones"]
            if m["id"] == "00-data-audit")
        actuate(out, decide(out, m0, r00))  # unlocks the depth sidequest
    assert (out / "portfolio" / "claims" / "00-data-audit.md").exists()
    assert (out / ".compile" / "status.json").exists()
    return out


def _tree_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


# ------------------------------------------------- the adversarial grep test

def case_publish_scrubs_fully_used_bundle(tmp):
    course = _used_course(tmp)
    template = publish_course(course, tmp / "template", author="dhruva")

    files = _tree_files(template)
    assert files, "template must not be empty"
    for marker in ("TOPICPROMPT9", "HWSTRING7", "ARTSENT3", "REFLSENT5"):
        hits = [str(p.relative_to(template)) for p in files
                if marker in p.read_text(errors="replace")]
        assert not hits, f"learner marker {marker} leaked into: {hits}"

    # Excluded classes are structurally absent — including every dotfile
    # except the grade workflow.
    names = {p.name for p in files}
    assert not names & {"events.jsonl", "grade.yaml", "artifact.md",
                        "reflection.md"}
    rels = {p.relative_to(template) for p in files}
    dotted = {r for r in rels if any(part.startswith(".") for part in r.parts)}
    assert dotted == {Path(".github/workflows/grade.yml")}, dotted
    assert not (template / ".compile").exists()
    assert not (template / "portfolio" / "claims").exists()

    # course.yaml: learner block gone, assumed-knowledge tags + publish
    # manifest present, provenance preserved.
    manifest = yaml.safe_load((template / "course.yaml").read_text())
    assert "learner" not in manifest
    assert "topic_prompt" not in manifest["meta"]
    cy_text = (template / "course.yaml").read_text()
    assert "hardware" not in cy_text and "weekly_hours" not in cy_text
    assert manifest["assumed_knowledge"] == ["spreadsheets"]
    assert manifest["target_artifact"], "artifact description is course content"
    pub = manifest["publish"]
    assert pub["author"] == "dhruva" and pub["license"] == "CC-BY-4.0"
    assert pub["sylabis_version"] and pub["published_at"]
    assert pub["content"] == "locators-only"
    sv = pub["source_verification"]
    assert sv["total"] == 2 and sv["flagged_search"] == 1 \
        and sv["unchecked"] == 1, sv
    assert "provenance" in manifest["meta"], "WS1a stamps must be preserved"
    assert "intake" in manifest["meta"]["provenance"]

    # Sidequests ship re-locked; the remedial milestone ships as content.
    for sq_yaml in template.glob("sidequests/*/sidequest.yaml"):
        sq = yaml.safe_load(sq_yaml.read_text())
        assert sq["locked"] is True and "unlocked_at" not in sq
    mids = [m["id"] for m in manifest["milestones"]]
    assert "00-data-audit-r1" in mids

    # Pristine portfolio + integrity hashes verify on every shipped doc.
    idx_meta, _ = okf.parse_doc(template / "portfolio" / "index.md")
    assert idx_meta["claim_count"] == 0
    assert okf.hash_problems(template) == []

    # The regenerated index must not dangle a link to the (excluded)
    # event log — the template ships no events.jsonl at all.
    assert "events.jsonl" not in (template / "index.md").read_text()

    # The publish itself is an event in the SOURCE course's log.
    published = [json.loads(l) for l in
                 (course / "events.jsonl").read_text().splitlines()
                 if json.loads(l)["type"] == "course.published"]
    assert len(published) == 1
    e = published[0]
    assert e["schema"] == 1 and all(k in e for k in ("id", "ts", "payload"))
    assert e["payload"]["license"] == "CC-BY-4.0"


# ------------------------------------------------- re-attach + startability

def case_template_reattaches_and_is_startable(tmp):
    course = _used_course(tmp)
    template = publish_course(course, tmp / "template")
    assert self_test(template) == [], "template must be shippable as-is"

    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    attached = journey.attach(home, str(template))
    assert attached in journey.course_dirs(home)
    step = journey.course_next(attached)
    assert step["status"] == "ready", step
    assert (attached / step["milestone_id"] / "LESSON.md").exists()
    # Nothing graded yet: the template starts from zero for its new learner.
    sub = journey.submittable(home)[0]
    assert sub["status"] == "awaiting_work" and sub["missing"]


# ----------------------------------------------------------- the gate cases

def case_planted_key_aborts_publish(tmp):
    course = _used_course(tmp)
    lesson = course / "00-data-audit" / "LESSON.md"
    lesson.write_text(lesson.read_text()
                      + "\n\nmy key: sk-ant-api03-AAAABBBBCCCC1234\n")
    dest = tmp / "template"
    try:
        publish_course(course, dest)
    except PublishError as e:
        assert "secret" in str(e) and "anthropic_api_key" in str(e)
        assert "sk-ant-api03" not in str(e), \
            "the refusal must not relay the secret itself"
    else:
        raise AssertionError("planted key must abort the publish")
    assert not dest.exists(), "a refused publish must leave nothing behind"


def case_gate_runs_self_test_on_template(tmp):
    course = _used_course(tmp)
    (course / "knowledge" / "index.md").unlink()  # corrupt the bundle
    dest = tmp / "template"
    try:
        publish_course(course, dest)
    except PublishError as e:
        assert "self-test" in str(e) and "knowledge/index.md" in str(e)
    else:
        raise AssertionError("a template failing self-test must not ship")
    assert not dest.exists()


def test_publish_requires_license(tmp):
    course = _used_course(tmp)
    for bad in ("", "   ", None):
        try:
            publish_course(course, tmp / "template", license=bad)
        except PublishError as e:
            assert "license" in str(e)
        else:
            raise AssertionError("license is mandatory")
    assert not (tmp / "template").exists()


def test_publish_refuses_dest_inside_bundle(tmp):
    course = _used_course(tmp)
    try:
        publish_course(course, course / "template")
    except PublishError as e:
        assert "outside" in str(e)
    else:
        raise AssertionError("dest inside the bundle would self-recurse")
    # And a dest that exists as a plain file is a typed refusal too.
    (tmp / "occupied").write_text("not a directory")
    try:
        publish_course(course, tmp / "occupied")
    except PublishError as e:
        assert "not a directory" in str(e)
    else:
        raise AssertionError("a file dest must refuse, not crash")
    assert (tmp / "occupied").read_text() == "not a directory"


def test_home_dotfiles_can_never_be_swept_in(tmp):
    """.session.json / .history are learner data living at the journey
    root, outside course dirs — and even a dotfile planted INSIDE the
    course dir must not survive the allowlist transform."""
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    (home / ".session.json").write_text('{"transcript": "SESSIONMARKER"}')
    (home / ".history").write_text("SESSIONMARKER\n")
    course = _used_course(tmp, out=home / "courses" / "survey")
    (course / ".env").write_text("ANTHROPIC_API_KEY=SESSIONMARKER\n")

    template = publish_course(course, tmp / "out")
    for p in _tree_files(template):
        assert "SESSIONMARKER" not in p.read_text(errors="replace"), p
    rels = {p.relative_to(template) for p in _tree_files(template)}
    dotted = {r for r in rels if any(s.startswith(".") for s in r.parts)}
    assert dotted == {Path(".github/workflows/grade.yml")}


# ------------------------------------------------------- gate units (direct)

def test_pii_audit_catches_every_excluded_class(tmp):
    bad = tmp / "bad"
    (bad / "00-x").mkdir(parents=True)
    (bad / "portfolio" / "claims").mkdir(parents=True)
    (bad / ".compile").mkdir()
    (bad / "events.jsonl").write_text("{}\n")
    (bad / "00-x" / "grade.yaml").write_text("passed: true\n")
    (bad / "00-x" / "artifact.md").write_text("work")
    (bad / "00-x" / "reflection.md").write_text("thoughts")
    (bad / "portfolio" / "claims" / "00-x.md").write_text("claim")
    (bad / ".compile" / "intake.json").write_text("{}")
    (bad / ".env").write_text("x")
    (bad / "course.yaml").write_text(yaml.dump({
        "meta": {"title": "T", "topic_prompt": "raw prompt"},
        "learner": {"hardware": "hw"},
        "milestones": []}))
    problems = "\n".join(pii_problems(bad))
    for expect in ("events.jsonl", "grade.yaml", "artifact.md",
                   "reflection.md", "claims", ".compile", ".env",
                   "learner block", "topic_prompt", "license"):
        assert expect in problems, f"PII audit must catch {expect}: {problems}"


def test_secret_scan_classes(tmp):
    d = tmp / "scan"
    d.mkdir()
    (d / "aws.md").write_text("key AKIAABCDEFGHIJKLMNOP in text")
    (d / "entropy.md").write_text("token aB3dE6gH9jK2mN5pQ8sT1vW4yZ7cF0iL end")
    (d / "assigned.md").write_text('password = "hunter2hunter2hunter123"')
    (d / "pem.md").write_text("-----BEGIN RSA PRIVATE KEY-----")
    hits = "\n".join(scan_secrets(d))
    for expect in ("aws_access_key_id", "high-entropy", "assigned_secret",
                   "private_key_block"):
        assert expect in hits, f"scan must catch {expect}: {hits}"

    clean = tmp / "clean"
    clean.mkdir()
    # sha256 hex (okf.yaml hashes) and the grade workflow's secret refs
    # are legitimate and must NOT trip the scan.
    (clean / "okf.yaml").write_text(
        "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b"
        "7852b855\n")
    (clean / "grade.yml").write_text(
        "env:\n  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n")
    assert scan_secrets(clean) == [], scan_secrets(clean)


# ------------------------------------------------------- integrity (hashes)

def case_okf_hashes_are_tamper_evident(tmp):
    course = _used_course(tmp)
    template = publish_course(course, tmp / "template")
    inventory = yaml.safe_load((template / "okf.yaml").read_text())
    assert inventory["documents"], "manifest must inventory the docs"
    for d in inventory["documents"]:
        assert d["sha256"] == okf.file_sha256(template / d["path"]), d["path"]
    assert okf.hash_problems(template) == []

    doc = template / "00-data-audit" / "LESSON.md"
    doc.write_text(doc.read_text() + "\ntampered\n")
    problems = okf.hash_problems(template)
    assert any("00-data-audit/LESSON.md" in p for p in problems), problems
