"""
Compiler pipeline: intake -> harvest -> sequence -> emit -> self-test.
Output is a course directory matching the course-template schema.

WS1a: every stage is checkpointed under <course_dir>/.compile/ so a
crashed compile resumes from the last completed stage instead of
re-buying earlier model calls. SHARED CONTRACT (the web surface polls
this for real progress): .compile/ holds one <stage>.json per completed
stage plus status.json:

    {"schema": 1, "stages": [<ordered stage names>], "done": [<completed>],
     "current": <stage-name or null>, "error": <string or null>}

status.json is updated at every stage boundary; on a successful compile
current is null and done lists every stage.

WS4.2 concurrency semantics, still within that shape: the independent
per-milestone lesson_<id> calls run under a bounded pool (LESSON_WORKERS).
While the pool is active, `current` is the pseudo-stage "lessons" and
`done` accumulates individual lesson_<id> entries as each completes
(always normalized to `stages` order, so status.json stays byte-
deterministic). On a lesson failure, `current` becomes that lesson's
stage name and `error` records it. Resume still skips completed lessons
one by one.

Harvest is propose -> verify -> consolidate: the model proposes candidate
sources, per-source verification runs concurrently (verify.py, cached in
.compile/verify_cache.json), and consolidation DROPS unverifiable
locators before sequencing (event: harvest.dropped). "Unverifiable"
means a resolvable locator (arxiv/doi/http) that failed its check;
search/opaque locators are kept-but-flagged exactly as before.
Floor: if more than half the course's sources would drop, a
compile.warning event is emitted and every source is kept (flagged)
instead of silently shipping a thin course. Verification itself never
blocks a compile.
"""
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from . import __version__
from . import events
from . import okf
from .errors import CompileDeclined, CompileError
from .llm import LLM, stage_model
from . import prompts
from .verify import verify_sources

# Ordered stage skeleton; lesson_<id> stages are inserted before "emit"
# once sequencing has named the milestones.
_BASE_STAGES = ["intake", "harvest", "sequence", "emit", "self_test"]

# Bounded concurrency for the independent per-milestone lesson calls —
# 3-4 keeps compile latency sublinear in course size without tripping
# rate limits (llm.py's retry/backoff covers the rest).
LESSON_WORKERS = 3

# WS4.2 seam — optional per-source agentic search loop. When a surface
# has a web-search tool configured it sets this to a callable:
#     hook(source: dict) -> str | None
# Consolidation invokes it for each source whose locator failed
# verification, BEFORE the drop decision; a returned locator replaces
# the failed one and is re-verified (through the same cache). Only a
# verified correction rescues the source. No live integration ships in
# Phase 1 — this is the seam only.
SEARCH_LOCATOR_HOOK = None


def profile_hash(profile: dict) -> str:
    """Stable hash of the learner block. Events record this, never the
    raw profile — a bundle must be publishable without leaking the
    learner's hardware/hours/priors (WS3b.3, at-source fix)."""
    canonical = json.dumps(profile, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Checkpoint:
    """Owns <course_dir>/.compile/ per the shared contract above.
    Thread-safe: run_parallel workers append to `done` concurrently, so
    every status mutation happens under one lock."""

    def __init__(self, course_dir: Path):
        self.dir = Path(course_dir) / ".compile"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.dir / "status.json"
        self._lock = threading.Lock()
        if self.status_path.exists():
            self.status = json.loads(self.status_path.read_text())
        else:
            self.status = {"schema": 1, "stages": [], "done": [],
                           "current": None, "error": None}
            self._write()

    def _write(self) -> None:
        # `done` accumulates in completion order, which is nondeterministic
        # under run_parallel — normalize to `stages` order so status.json
        # is identical however the pool interleaves.
        order = {s: i for i, s in enumerate(self.status["stages"])}
        self.status["done"].sort(key=lambda s: order.get(s, len(order)))
        self.status_path.write_text(json.dumps(self.status, indent=2))

    def set_stages(self, stages: list[str]) -> None:
        with self._lock:
            self.status["stages"] = list(stages)
            self._write()

    def is_done(self, stage: str) -> bool:
        return stage in self.status["done"]

    def is_complete(self) -> bool:
        stages = self.status["stages"]
        return bool(stages) and all(s in self.status["done"] for s in stages)

    def load(self, stage: str):
        return json.loads((self.dir / f"{stage}.json").read_text())

    def _save(self, stage: str, result) -> None:
        (self.dir / f"{stage}.json").write_text(json.dumps(result))
        with self._lock:
            self.status["done"].append(stage)
            self._write()

    def _record_failure(self, stage: str, exc: BaseException,
                        reset_current: bool = False) -> None:
        with self._lock:
            if not self.status["error"]:  # innermost failure wins
                self.status["error"] = f"{stage}: {exc}"
                if reset_current:
                    self.status["current"] = stage
            self._write()

    def run(self, stage: str, fn):
        """Run one stage exactly once: a completed stage returns its
        saved output without calling fn (so re-runs never repeat paid
        model calls); a failure records the error in status.json and
        re-raises."""
        if self.is_done(stage):
            return self.load(stage)
        with self._lock:
            if stage not in self.status["stages"]:
                self.status["stages"].append(stage)
            self.status["current"] = stage
            self.status["error"] = None
            self._write()
        try:
            result = fn()
        except BaseException as e:
            self._record_failure(stage, e)
            raise
        self._save(stage, result)
        with self._lock:
            self.status["current"] = None
            self._write()
        return result

    def run_parallel(self, jobs: list[tuple[str, object]], workers: int,
                     label: str) -> dict:
        """Run independent stages with bounded concurrency. Same
        exactly-once semantics as run(): completed stages load from disk
        without calling their fn, each finished stage checkpoints its own
        <stage>.json and joins `done` immediately. While the pool is
        active `current` is `label` (pseudo-stage, string-or-null shape
        preserved); on failure `current` points at the failed stage, the
        first failure in job order re-raises after the pool drains, and
        stages that finished anyway stay checkpointed for the resume."""
        results = {}
        pending = []
        for stage, fn in jobs:
            if self.is_done(stage):
                results[stage] = self.load(stage)
            else:
                pending.append((stage, fn))
        if not pending:
            return results
        with self._lock:
            for stage, _ in pending:
                if stage not in self.status["stages"]:
                    self.status["stages"].append(stage)
            self.status["current"] = label
            self.status["error"] = None
            self._write()

        def _worker(stage, fn):
            try:
                result = fn()
            except BaseException as e:
                self._record_failure(stage, e, reset_current=True)
                raise
            self._save(stage, result)
            return result

        first_error = None
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [(stage, pool.submit(_worker, stage, fn))
                       for stage, fn in pending]
            for stage, fut in futures:  # job order => deterministic raise
                try:
                    results[stage] = fut.result()
                except BaseException as e:
                    if first_error is None:
                        first_error = e
        if first_error is not None:
            raise first_error
        with self._lock:
            self.status["current"] = None
            self._write()
        return results


def compile_course(topic: str, profile: dict, out_dir: Path, llm: LLM) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ck = _Checkpoint(out_dir)
    if ck.is_complete():
        print(f"Course already compiled: {out_dir}")
        return out_dir
    fresh = not ck.status["done"]
    if not ck.status["stages"]:
        ck.set_stages(_BASE_STAGES)
    if fresh:
        events.emit(out_dir, "compile.requested",
                    {"topic": topic,
                     "learner_profile_hash": profile_hash(profile)})

    prev_usage_dir = getattr(llm, "usage_dir", None)
    llm.usage_dir = out_dir  # per-stage token/cost events -> events.jsonl
    try:
        # Stage 1 — Intake (includes viability check)
        print("[1/5] Intake + viability...")
        spec = ck.run("intake", lambda: _intake(topic, profile, llm))
        print(f"      domain={spec['domain']} "
              f"checkable={spec['viability']['verifiable_skeleton_pct']}% "
              f"grader={spec['viability']['grader_mode']}")

        # Stage 2 — Harvest: propose -> verify (concurrent, cached) ->
        # consolidate (drop unverifiable before sequencing; never blocks)
        print("[2/5] Source harvest...")
        harvest = ck.run("harvest", lambda: _harvest(spec, llm, out_dir, ck))
        print(f"      {len(harvest['sources'])} sources shipped")

        # Stage 3 — Sequence
        print("[3/5] Sequencing...")
        seq = ck.run("sequence",
                     lambda: _sequence(spec, harvest, llm, out_dir))
        _check_dag(seq["milestones"])
        print(f"      {len(seq['milestones'])} milestones, "
              f"{len(seq.get('sidequests', []))} sidequests")

        # Now the full stage list is known — lesson stages before emit.
        lesson_stages = [f"lesson_{m['id']}" for m in seq["milestones"]]
        ck.set_stages(["intake", "harvest", "sequence",
                       *lesson_stages, "emit", "self_test"])

        # Stage 4 — Lessons (independent per-milestone calls, bounded
        # pool; each checkpoints individually) then emit the repo.
        print("[4/5] Emitting course repo...")
        lessons = _generate_lessons(spec, harvest, seq, llm, ck)
        ck.run("emit", lambda: _emit_repo(out_dir, topic, spec, harvest,
                                          seq, lessons) or {"ok": True})

        # Stage 5 — Self-test (structural)
        print("[5/5] Self-test...")
        ck.run("self_test", lambda: _self_test_stage(out_dir))
    finally:
        llm.usage_dir = prev_usage_dir

    events.emit(out_dir, "compile.completed", {
        "milestone_count": len(seq["milestones"]),
        "viability_pct": spec["viability"]["verifiable_skeleton_pct"],
        "grader_mode": spec["viability"]["grader_mode"],
        "okf_conformant": True,  # gated: self-test failure never reaches here
    })
    print(f"\nCourse compiled: {out_dir}")
    return out_dir


def _intake(topic: str, profile: dict, llm: LLM) -> dict:
    spec = llm.call_json("intake", prompts.INTAKE_SYSTEM,
                         f"Topic: {topic}\nLearner profile: {profile}")
    if spec["viability"]["verdict"] == "decline":
        notes = spec["viability"].get("notes", "")
        raise CompileDeclined(f"Declined: {notes}",
                              verdict="decline", notes=notes)
    return spec


def _load_verify_cache(compile_dir: Path) -> dict:
    p = Path(compile_dir) / "verify_cache.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_verify_cache(compile_dir: Path, cache: dict) -> None:
    (Path(compile_dir) / "verify_cache.json").write_text(
        json.dumps(cache, indent=2))


def _harvest(spec: dict, llm: LLM, out_dir: Path, ck: _Checkpoint) -> dict:
    """Propose -> verify -> consolidate. The checkpointed result is the
    CONSOLIDATED harvest, so a resume never revisits the drop decision."""
    harvest = llm.call_json("harvest", prompts.HARVEST_SYSTEM,
                            f"Compilation spec: {spec}")
    cache = _load_verify_cache(ck.dir)
    vsum = verify_sources(harvest["sources"], enabled=not llm.mock,
                          cache=cache)
    _save_verify_cache(ck.dir, cache)
    print(f"      {vsum['verified']} verified, {vsum['unverified']} unverified, "
          f"{vsum['flagged_search']} flagged search, "
          f"{vsum['skipped']} unchecked")

    def reverify(source: dict) -> None:
        verify_sources([source], enabled=not llm.mock, cache=cache)
        _save_verify_cache(ck.dir, cache)

    return _consolidate_sources(harvest, out_dir, reverify)


def _consolidate_sources(harvest: dict, out_dir: Path, reverify) -> dict:
    """Drop unverifiable locators before sequencing. Unverifiable = a
    resolvable locator (arxiv/doi/http) whose check failed; search and
    opaque locators stay kept-but-flagged. Every drop is event-logged
    (harvest.dropped). Floor: if more than half the course's sources
    would drop, keep everything flagged and warn instead — a thin course
    shipped silently is worse than a flagged one."""
    sources = harvest["sources"]
    unverifiable = [s for s in sources
                    if s.get("verified") is False
                    and s.get("locator_kind") in ("arxiv", "doi", "http")]

    hook = SEARCH_LOCATOR_HOOK
    if hook is not None and unverifiable:
        rescued = set()
        for s in unverifiable:
            corrected = hook(s)
            if corrected and corrected != s["locator"]:
                s["locator"] = corrected
                reverify(s)
            if s.get("verified") is True:
                rescued.add(id(s))
        unverifiable = [s for s in unverifiable if id(s) not in rescued]

    if not unverifiable:
        return harvest
    if len(unverifiable) * 2 > len(sources):
        events.emit(out_dir, "compile.warning", {
            "stage": "harvest",
            "reason": "verification_drop_floor",
            "total_sources": len(sources),
            "unverifiable": [s.get("id") for s in unverifiable],
        })
        print(f"      WARNING: {len(unverifiable)}/{len(sources)} sources "
              f"unverifiable — keeping all sources flagged instead of "
              f"shipping a thin course")
        return harvest

    dropped_ids = {id(s) for s in unverifiable}
    for s in unverifiable:
        events.emit(out_dir, "harvest.dropped", {
            "source_id": s.get("id"),
            "locator": s.get("locator"),
            "locator_kind": s.get("locator_kind"),
            "verification": s.get("verification"),
        })
    print(f"      dropped {len(unverifiable)} unverifiable source(s) "
          f"before sequencing")
    return {**harvest,
            "sources": [s for s in sources if id(s) not in dropped_ids]}


def _sequence(spec: dict, harvest: dict, llm: LLM, out_dir: Path) -> dict:
    seq = llm.call_json("sequence", prompts.SEQUENCE_SYSTEM,
                        f"Spec: {spec}\nSources: {harvest}")
    # Milestone-level floor at sequencing time: sequencing only saw the
    # consolidated sources, so any unknown source_id means the milestone
    # leans on material the course cannot ship. Losing more than half of
    # a milestone's sources is worth a warning (lesson emission already
    # filters to known ids).
    known = {s["id"] for s in harvest["sources"]}
    for m in seq.get("milestones", []):
        refs = m.get("source_ids", [])
        missing = [sid for sid in refs if sid not in known]
        if missing and len(missing) * 2 > len(refs):
            events.emit(out_dir, "compile.warning", {
                "stage": "sequence",
                "reason": "milestone_source_floor",
                "milestone_id": m.get("id"),
                "missing_source_ids": missing,
            })
    return seq


def _self_test_stage(course_dir: Path) -> dict:
    problems = self_test(course_dir)
    if problems:
        raise CompileError("Self-test FAILED — course not shipped:\n" +
                           "\n".join(f"  - {p}" for p in problems))
    return {"problems": []}


def compile_remedial(course_dir: Path, parent_id: str, concept: str,
                     llm: LLM) -> str:
    """Mini compile: one supplementary micro-milestone targeting a specific
    misconception, inserted BEFORE the failed milestone so `next` serves it
    first. Reuses the lesson prompt with a tighter spec."""
    course_dir = Path(course_dir)
    manifest = yaml.safe_load((course_dir / "course.yaml").read_text())
    idx, parent = next((i, m) for i, m in enumerate(manifest["milestones"])
                       if m["id"] == parent_id)
    n = 1 + sum(1 for m in manifest["milestones"]
                if m["id"].startswith(f"{parent_id}-r"))
    rid = f"{parent_id}-r{n}"
    milestone = {
        "id": rid,
        "title": f"Remedial — {concept}",
        "estimated_hours": 1,
        "artifact_type": "reflection",
        "artifact_spec": ("A short reflection demonstrating corrected "
                          f"understanding of {concept}"),
        "source_ids": [],
        "core_concepts": [concept],
        "misconception_target": parent.get("misconception_target", concept),
        "depends_on": parent.get("depends_on", []),
    }
    course_title = manifest["meta"]["title"]
    fm_block = okf.milestone_frontmatter(milestone, course_title)
    lesson = llm.call(
        f"lesson_{rid}", prompts.LESSON_SYSTEM,
        "Remedial micro-module (1 hour, reflection-only artifact). "
        f"Tighter spec: concept={concept!r}, "
        f"misconception={milestone['misconception_target']!r}, "
        f"milestone_parent={parent_id!r}\n"
        f"Milestone: {milestone}\n"
        f"OKF frontmatter block (prepend verbatim):\n{fm_block}")
    okf.emit_milestone_doc(course_dir, milestone, course_title, lesson)

    checkpoint = {
        "milestone_id": rid,
        "artifact_type": "reflection",
        "artifact_spec": milestone["artifact_spec"],
        "grader_type": "three_tier",
        # reflection-only: Tier 2 has no artifact to audit and skips itself
        "structural": {"required_files": ["reflection.md"]},
        "misconception_target": milestone["misconception_target"],
        "core_concepts": [concept],
        "pass_threshold": 0.75,
    }
    (course_dir / rid).mkdir(exist_ok=True)
    (course_dir / rid / "checkpoint.yaml").write_text(
        yaml.dump(checkpoint, default_flow_style=False))

    manifest["milestones"].insert(idx, {
        "id": rid, "title": milestone["title"],
        "estimated_hours": 1, "artifact_type": "reflection",
        "checkpoint": f"{rid}/checkpoint.yaml",
        "misconception_target": milestone["misconception_target"],
        "sidequests": {}, "depends_on": milestone["depends_on"],
    })
    (course_dir / "course.yaml").write_text(
        yaml.dump(manifest, default_flow_style=False))
    okf.emit_course_index(course_dir)
    okf.emit_bundle_manifest(course_dir)
    events.emit(course_dir, "remedial.injected",
                {"remedial_id": rid, "parent_milestone_id": parent_id,
                 "concept": concept})
    return rid


def _check_dag(milestones: list) -> None:
    ids = {m["id"] for m in milestones}
    seen = set()
    for m in milestones:  # milestones arrive in order; deps must precede
        for dep in m.get("depends_on", []):
            if dep not in ids:
                raise CompileError(
                    f"Sequencing error: {m['id']} depends on unknown {dep}")
            if dep not in seen:
                raise CompileError(
                    f"Sequencing error: {m['id']} depends on later milestone {dep}")
        seen.add(m["id"])


def _provenance() -> dict:
    """Per-stage {model, prompt_version, sylabis_version} stamps for
    course.yaml meta — a shared bundle must be traceable to what
    produced it (registry prerequisite, WS3)."""
    return {family: {"model": stage_model(family),
                     "prompt_version": prompts.prompt_version(family),
                     "sylabis_version": __version__}
            for family in ("intake", "harvest", "sequence", "lesson")}


def _generate_lessons(spec: dict, harvest: dict, seq: dict, llm: LLM,
                      ck: _Checkpoint) -> dict:
    """The per-milestone lesson calls are independent of one another, so
    they run under a bounded pool (LESSON_WORKERS) — compile latency
    stops being linear in course size. Each lesson still checkpoints
    individually (resume skips completed ones). Only the MODEL CALLS run
    in workers; all bundle emission stays serial in _emit_repo, so okf.py
    is never entered concurrently and the resulting bundle is identical
    whatever order the pool finishes in. Returns {milestone_id: body}."""
    course_title = spec["topic"]
    jobs = []
    for m in seq["milestones"]:
        m_sources = [s for s in harvest["sources"] if s["id"] in m["source_ids"]]
        fm_block = okf.milestone_frontmatter(m, course_title)
        jobs.append((
            f"lesson_{m['id']}",
            lambda m=m, m_sources=m_sources, fm_block=fm_block: llm.call(
                f"lesson_{m['id']}", prompts.LESSON_SYSTEM,
                f"Spec: {spec}\nMilestone: {m}\nSources: {m_sources}\n"
                f"OKF frontmatter block (prepend verbatim):\n{fm_block}")))
    bodies = ck.run_parallel(jobs, workers=LESSON_WORKERS, label="lessons")
    return {m["id"]: bodies[f"lesson_{m['id']}"] for m in seq["milestones"]}


def _emit_repo(out: Path, topic: str, spec: dict, harvest: dict,
               seq: dict, lessons: dict) -> None:
    grader_mode = spec["viability"]["grader_mode"]

    # course.yaml
    manifest = {
        "meta": {"title": spec["topic"], "domain": spec["domain"],
                 "version": __version__,
                 "compiled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "topic_prompt": topic,
                 "provenance": _provenance()},
        "learner": spec.get("constraints", {}) | {
            "prior_knowledge": spec.get("assumed_knowledge", []),
            "target_artifact": spec["target_artifact"]},
        "viability": spec["viability"],
        "milestones": [],
    }

    sq_by_parent = {}
    for sq in seq.get("sidequests", []):
        sq_by_parent.setdefault(sq["parent"], {})[sq["type"]] = sq["id"]
        sq_dir = out / "sidequests" / sq["id"]
        sq_dir.mkdir(parents=True, exist_ok=True)
        (sq_dir / "sidequest.yaml").write_text(yaml.dump(
            {**sq, "locked": True}, default_flow_style=False))

    course_title = spec["topic"]
    for m in seq["milestones"]:
        m_dir = out / m["id"]
        (m_dir / "starter").mkdir(parents=True, exist_ok=True)

        # Lesson bodies were generated (and checkpointed per milestone)
        # by _generate_lessons; emission stays serial and in milestone
        # order so the bundle is deterministic and okf.py — the only
        # frontmatter writer — runs on one thread.
        # okf normalizes the frontmatter on receipt, so a model that
        # reformats the echoed block cannot break conformance.
        okf.emit_milestone_doc(out, m, course_title, lessons[m["id"]])

        checkpoint = {
            "milestone_id": m["id"],
            "artifact_type": m["artifact_type"],
            "artifact_spec": m["artifact_spec"],
            "grader_type": grader_mode,
            "structural": {"required_files": ["artifact.md", "reflection.md"]
                           if grader_mode == "three_tier"
                           else ["reflection.md"]},
            "misconception_target": m["misconception_target"],
            "core_concepts": m["core_concepts"],
            "pass_threshold": 0.75,
        }
        if grader_mode in ("three_tier", "hybrid"):
            checkpoint["tier_3_rubric"] = {
                # Stays disabled until a human seeds the exemplar set
                # (15-20 annotated artifacts); the grader falls back to the
                # claim-audit-derived score while disabled.
                "enabled": False,
                "exemplars_dir": f"grader/exemplars/{m['id']}",
                "dimensions": _rubric_dimensions(m),
            }
        if grader_mode in ("executable", "hybrid"):
            checkpoint["rubric"] = {"scripts": ["starter/run_benchmark.py"]}
            (m_dir / "starter" / "run_benchmark.py").write_text(
                _BENCHMARK_TEMPLATE
                .replace("__MILESTONE__", m["id"])
                .replace("__ARTIFACT_SPEC__", m["artifact_spec"]))
        (m_dir / "checkpoint.yaml").write_text(
            yaml.dump(checkpoint, default_flow_style=False))

        manifest["milestones"].append({
            "id": m["id"], "title": m["title"],
            "estimated_hours": m["estimated_hours"],
            "artifact_type": m["artifact_type"],
            "checkpoint": f"{m['id']}/checkpoint.yaml",
            "misconception_target": m["misconception_target"],
            "sidequests": sq_by_parent.get(m["id"], {}),
            "depends_on": m.get("depends_on", []),
        })

    (out / "course.yaml").write_text(yaml.dump(manifest, default_flow_style=False))
    # sources.yaml is the machine-readable fallback for grader scripts;
    # knowledge/ is the canonical OKF representation. Duplication intended.
    (out / "grader").mkdir(exist_ok=True)
    (out / "grader" / "sources.yaml").write_text(
        yaml.dump(harvest, default_flow_style=False))
    (out / "portfolio").mkdir(exist_ok=True)
    (out / "portfolio" / "state.yaml").write_text(
        yaml.dump({"entries": []}, default_flow_style=False))

    # GitHub-as-UI: the bundle repo grades itself on push (P1). Requires
    # the learner's repo to define secret ANTHROPIC_API_KEY (and optionally
    # var SYLABIS_GIT_URL); see comments inside the workflow.
    wf_dir = out / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "grade.yml").write_text(_GRADE_WORKFLOW)

    okf.emit_knowledge_bundle(out, harvest, course_title)
    okf.emit_portfolio_index(out)
    okf.emit_course_index(out)
    okf.emit_bundle_manifest(out)  # inventories every OKF doc; emit last


def _rubric_dimensions(m: dict) -> list[dict]:
    dims = [{"name": "artifact_fidelity",
             "description": f"Meets the artifact spec: {m['artifact_spec']}"}]
    dims += [{"name": c, "description": f"Demonstrates working command of {c}"}
             for c in m.get("core_concepts", [])]
    return dims


_GRADE_WORKFLOW = '''\
# sylabis grader — emitted into every compiled course bundle.
# GitHub IS the UI: push artifact.md + reflection.md, get graded feedback
# as a commit (push) or a PR comment (pull request).
#
# One-time repo setup:
#   1. Settings -> Secrets: add ANTHROPIC_API_KEY
#   2. (optional) Settings -> Variables: SYLABIS_GIT_URL to pin the
#      compiler source; defaults below.
name: grade

on:
  push:
    paths: ["*/artifact.md", "*/reflection.md"]
  pull_request:
    paths: ["*/artifact.md", "*/reflection.md"]

permissions:
  contents: write
  pull-requests: write

concurrency:
  group: grade-${{ github.ref }}
  cancel-in-progress: false

jobs:
  grade:
    runs-on: ubuntu-latest
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install sylabis
        run: pip install "${{ vars.SYLABIS_GIT_URL || 'git+https://github.com/dhruvakrishnan/sylabis' }}"

      - name: Find changed milestones
        id: changed
        run: |
          if [ "${{ github.event_name }}" = "pull_request" ]; then
            BASE="${{ github.event.pull_request.base.sha }}"
          else
            BASE="${{ github.event.before }}"
          fi
          git cat-file -e "$BASE" 2>/dev/null || BASE=$(git hash-object -t tree /dev/null)
          MIDS=$(git diff --name-only "$BASE" HEAD -- '*/artifact.md' '*/reflection.md' \\
                 | cut -d/ -f1 | sort -u | tr '\\n' ' ')
          echo "mids=$MIDS" >> "$GITHUB_OUTPUT"

      - name: Grade changed milestones
        id: grade
        run: |
          : > /tmp/grade-report.md
          FAILED=0
          for MID in ${{ steps.changed.outputs.mids }}; do
            echo "## $MID" >> /tmp/grade-report.md
            echo '```'    >> /tmp/grade-report.md
            if ! sylabis grade . "$MID" >> /tmp/grade-report.md 2>&1; then
              FAILED=1
            fi
            echo '```'    >> /tmp/grade-report.md
          done
          cat /tmp/grade-report.md
          echo "failed=$FAILED" >> "$GITHUB_OUTPUT"

      - name: Post feedback as PR comment
        if: github.event_name == 'pull_request'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh pr comment "${{ github.event.pull_request.number }}" --body-file /tmp/grade-report.md

      - name: Commit grade state
        if: github.event_name == 'push'
        run: |
          git config user.name "sylabis-grader"
          git config user.email "grader@users.noreply.github.com"
          git add -A
          git commit -m "grade: ${{ steps.changed.outputs.mids }}" || echo "nothing to commit"
          git push

      - name: Fail the check when a milestone did not pass
        if: steps.grade.outputs.failed == '1'
        run: exit 1
'''

_BENCHMARK_TEMPLATE = '''\
"""
Rubric benchmark for __MILESTONE__. Fill in the TODOs, then run:

    python starter/run_benchmark.py

The grader runs this script in the milestone directory and reads ONE JSON
object from stdout:

    {"score": <float 0.0-1.0>, "metrics": {...}, "notes": "<optional>"}

Artifact spec: __ARTIFACT_SPEC__
"""
import json


def run() -> dict:
    metrics = {}
    # TODO: exercise your artifact and populate metrics
    # (e.g. tokens/sec, peak memory, accuracy on your eval set),
    # then derive a score in [0.0, 1.0] from them.
    raise NotImplementedError("fill in run() before grading")
    return {"score": 0.0, "metrics": metrics, "notes": ""}


if __name__ == "__main__":
    print(json.dumps(run()))
'''


def self_test(course_dir: Path) -> list[str]:
    """Structural consistency + OKF conformance. A failing course does not
    ship — no skip flag, ever."""
    problems = []
    course_dir = Path(course_dir)
    manifest_path = course_dir / "course.yaml"
    if not manifest_path.exists():
        return ["course.yaml missing"]
    manifest = yaml.safe_load(manifest_path.read_text())
    problems.extend(okf.conformance_problems(course_dir))

    for m in manifest.get("milestones", []):
        m_dir = course_dir / m["id"]
        if not (m_dir / "LESSON.md").exists():
            problems.append(f"{m['id']}: LESSON.md missing")
        cp = m_dir / "checkpoint.yaml"
        if not cp.exists():
            problems.append(f"{m['id']}: checkpoint.yaml missing")
        else:
            c = yaml.safe_load(cp.read_text())
            for field in ("grader_type", "misconception_target", "core_concepts"):
                if not c.get(field):
                    problems.append(f"{m['id']}: checkpoint missing {field}")
        for sq_path in (m.get("sidequests") or {}).values():
            if not (course_dir / "sidequests" / sq_path).exists():
                problems.append(f"{m['id']}: sidequest {sq_path} referenced but missing")
    return problems
