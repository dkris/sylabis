"""
Cross-wave seam regressions (found by the Phase-1 integration audit).

The unscored-grade path (WS4.3: a rubric-less executable/hybrid checkpoint
yields tier_3_mode='unscored' with the grade key omitted, never a fabricated
0.85) crosses the grader, journey, okf, and the surfaces. The green wave
suites exercised the grader's numeric behavior but never the *display* and
*linking* of an unscored pass — these tests pin that seam.
"""
import io
import yaml
from contextlib import redirect_stdout
from pathlib import Path

from sylabis import journey, okf
from sylabis.llm import LLM

from tests.run_all import (compile_mock, submit, grade_mock,
                           STRONG_ARTIFACT, STRONG_REFLECTION)


def _make_unscored(course: Path, mid: str = "00-data-audit") -> None:
    """Turn a milestone into a rubric-less executable checkpoint — the exact
    WS4.3a shape that must grade 'unscored', not 0.85."""
    cp_path = course / mid / "checkpoint.yaml"
    cp = yaml.safe_load(cp_path.read_text())
    cp["grader_type"] = "executable"
    cp["rubric"] = {"scripts": []}
    cp_path.write_text(yaml.dump(cp))


def test_unscored_pass_omits_grade_and_flags(tmp):
    course = compile_mock(tmp)
    _make_unscored(course)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    r = grade_mock(course, "00-data-audit")
    assert r["passed"], "tiers 1 + explain-back should still pass strong work"
    assert r.get("grade") is None and r.get("unscored") is True
    assert r["tier_3_mode"] == "unscored"
    gy = yaml.safe_load((course / "00-data-audit" / "grade.yaml").read_text())
    assert "grade" not in gy, "no fabricated number on disk"
    assert gy.get("unscored") is True
    # No scored claim doc is minted for an unscored pass.
    assert not (course / "portfolio" / "claims" / "00-data-audit.md").exists()


def test_grade_token_renders_unscored_not_zero_percent(tmp):
    # The bug the audit caught: `.get('grade', 0):.0%` shows a pass as "0%".
    assert okf.grade_token({"passed": True, "unscored": True}) == "unscored"
    assert okf.grade_token({"passed": True}) == "unscored"
    assert okf.grade_token({"passed": True, "grade": 0.91}) == "91%"
    assert okf.grade_token({"passed": True, "grade": 0.0}) == "0%"


def test_okf_milestone_status_no_zero_percent_for_unscored(tmp):
    course = compile_mock(tmp)
    _make_unscored(course)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(course, "00-data-audit")
    status = okf._milestone_status(course, "00-data-audit")
    assert status.startswith("passed")
    assert "unscored" in status and "0%" not in status


def test_knowledge_map_links_only_when_claim_exists(tmp):
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    course = home / "courses" / "survey"
    with redirect_stdout(io.StringIO()):
        from sylabis.compiler import compile_course
        compile_course("Survey synthesis", {"weekly_hours": 5}, course,
                       LLM(mock=True))
    _make_unscored(course)
    submit(course, "00-data-audit", STRONG_ARTIFACT, STRONG_REFLECTION)
    grade_mock(course, "00-data-audit")

    know = journey.knowledge(home)
    rows = [ev for e in know for ev in e["evidence"]
            if ev["milestone_id"] == "00-data-audit"]
    assert rows, "the unscored pass still verifies concepts"
    assert all(ev["has_claim"] is False for ev in rows)

    # emit_map must not link to a claim doc that was never written.
    journey.emit_map(home)
    kmap = (home / "knowledge.md").read_text()
    assert "portfolio/claims/00-data-audit.md" not in kmap
    assert "(unscored)" in kmap
