"""
WS3c suite: the registry client + the journey-page publisher.

All offline and deterministic: the registry index is a file:// fixture,
bundle repos are real local git repos (git init/commit gives real
pinned SHAs), the model is the fixture mock, and the fetch/clock seams
of the cache are injectable. Covers, per the acceptance criteria:

- search/get against a file:// registry end-to-end: publish a mock
  course, list it in a temp paths.json, `get` attaches at the PINNED
  sha (an extra post-listing commit exists and must NOT be what
  attaches), a milestone completes, grading runs;
- a tampered post-listing repo (history rewritten so the listed sha is
  gone; a bogus sha in the entry) refused with AttachError/RegistryError;
- cache TTL + ETag honored via injectable clock/fetcher — within the
  TTL the fetcher is never called (the no-telemetry GET is also the
  ONLY request);
- `sy journey --publish` output contains concepts + grades but zero
  artifact/reflection text, learner fields, or journey-home paths
  (adversarial grep), and a page that WOULD leak is refused.
"""
import io
import json
import os
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from sylabis import cli, journey, okf, registry
from sylabis.compiler import compile_course
from sylabis.errors import AttachError, PublishError, RegistryError
from sylabis.grader import grade
from sylabis.llm import LLM
from sylabis.publish import publish_course

FIX = Path(__file__).parent.parent / "fixtures"

# Distinctive learner markers — none may reach a public journey page.
TOPIC = "TOPICPROMPT9 synthesize surveys on my attic rig"
HW = "HWSTRING7-rtx-6000-in-the-attic"
ARTIFACT = ("ARTSENT3 61% of respondents (n=140) reported satisfaction. "
            "Findings cannot be generalized to SMB customers.")
REFLECTION = ("REFLSENT5 I wanted the data to support more than it can; "
              "the skew is a generalization boundary.")


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    return home


def _compile(tmp: Path, out: Path, topic: str = "Survey synthesis",
             fdir: Path | None = None, hardware: str = "") -> Path:
    with redirect_stdout(io.StringIO()):
        compile_course(topic, {"weekly_hours": 5, "hardware": hardware,
                               "prior_knowledge": ["spreadsheets"]},
                       out, LLM(mock=True, fixtures_dir=fdir or FIX))
    return out


def _template_repo(tmp: Path) -> tuple[Path, str]:
    """A published template in a real git repo: (repo path, listed sha).
    A SECOND commit exists on top of the listed one, so a test can prove
    `get` checked out the pin rather than HEAD."""
    course = _compile(tmp, tmp / "course")
    template = publish_course(course, tmp / "template", author="tester")
    _git(template, "init", "-q")
    _git(template, "add", "-A")
    _git(template, "commit", "-qm", "listed")
    sha = _git(template, "rev-parse", "HEAD")
    (template / "index.md").write_text(
        (template / "index.md").read_text() + "\nPOSTLISTING-EDIT\n")
    _git(template, "add", "-A")
    _git(template, "commit", "-qm", "after listing")
    return template, sha


def _index_url(tmp: Path, entries: list[dict]) -> str:
    p = tmp / "paths.json"
    p.write_text(json.dumps({"schema": 1, "paths": entries}))
    return f"file://{p}"


def _entry(repo: Path, sha: str, **over) -> dict:
    return {"id": "tester/survey-synthesis",
            "name": "Survey Synthesis for Practitioners",
            "git_url": f"file://{repo}", "commit_sha": sha,
            "topic": "survey methods and synthesis", "est_hours": 5,
            "license": "CC-BY-4.0", "assumed_knowledge": ["spreadsheets"],
            "author": "tester", **over}


def _grade_mock(course: Path, mid: str) -> dict:
    with redirect_stdout(io.StringIO()):
        return grade(course, mid, LLM(mock=True))


# ------------------------------------------------- search/get end to end

def case_registry_search_get_end_to_end(tmp):
    home = _home(tmp)
    repo, sha = _template_repo(tmp)
    url = _index_url(tmp, [_entry(repo, sha)])

    hits = registry.search(home, "survey", url=url)
    assert [e["id"] for e in hits] == ["tester/survey-synthesis"]
    assert registry.search(home, "spreadsheet", url=url), \
        "assumed-knowledge tags must be searchable"
    assert registry.search(home, "SYNTHESIS", url=url), \
        "search must be case-insensitive"
    assert registry.search(home, "no-such-topic", url=url) == []
    assert len(registry.search(home, "", url=url)) == 1, \
        "empty query lists everything"

    dest = registry.get(home, "tester/survey-synthesis", url=url)
    assert dest in journey.course_dirs(home)
    info = journey.attach_info(dest)
    assert info["commit"] == sha, "attach must record the PINNED commit"
    assert "POSTLISTING-EDIT" not in (dest / "index.md").read_text(), \
        "get must check out the pinned sha, not the repo HEAD"

    # The attached path is immediately usable: complete a milestone,
    # grading runs, knowledge counts.
    step = journey.course_next(dest)
    assert step["status"] == "ready" and step["milestone_id"] == "00-data-audit"
    (dest / "00-data-audit" / "artifact.md").write_text(ARTIFACT)
    (dest / "00-data-audit" / "reflection.md").write_text(REFLECTION)
    r = _grade_mock(dest, "00-data-audit")
    assert r["passed"]
    concepts = {e["concept"] for e in journey.knowledge(home)}
    assert "sampling frame" in concepts, \
        "knowledge earned in a registry path must join the journey"


# --------------------------------------------------- tampering is refused

def case_tampered_listing_refused(tmp):
    home = _home(tmp)
    repo, sha = _template_repo(tmp)

    # Post-listing history rewrite: the whole history is re-created, so
    # a fresh clone (which only carries reachable history) no longer
    # contains the listed sha.
    shutil.rmtree(repo / ".git")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "history rewritten")
    assert _git(repo, "rev-parse", "HEAD") != sha
    url = _index_url(tmp, [_entry(repo, sha)])
    before = journey.course_dirs(home)
    try:
        registry.get(home, "tester/survey-synthesis", url=url)
    except AttachError as e:
        assert "sha mismatch" in str(e), e
    else:
        raise AssertionError("a rewritten repo must be refused")
    assert journey.course_dirs(home) == before, \
        "a refused attach must leave nothing in the journey"

    # A bogus pinned sha (index tampering) is the same refusal.
    url = _index_url(tmp, [_entry(repo, "deadbeef" * 5)])
    try:
        registry.get(home, "tester/survey-synthesis", url=url, force=True)
    except AttachError as e:
        assert "sha mismatch" in str(e)
    else:
        raise AssertionError("a bogus sha must be refused")

    # Unknown id and an unpinned listing are typed registry errors.
    try:
        registry.get(home, "nobody/ghost", url=url, force=True)
    except RegistryError as e:
        assert "ghost" in str(e)
    else:
        raise AssertionError("unknown id must raise RegistryError")
    url = _index_url(tmp, [_entry(repo, "")])
    try:
        registry.get(home, "tester/survey-synthesis", url=url, force=True)
    except RegistryError as e:
        assert "pinned" in str(e)
    else:
        raise AssertionError("an unpinned listing must be refused")


def test_attach_pin_requires_git_source(tmp):
    home = _home(tmp)
    course = _compile(tmp, tmp / "course")
    try:
        journey.attach(home, str(course), pin_sha="deadbeef" * 5)
    except AttachError as e:
        assert "git" in str(e)
    else:
        raise AssertionError("pin_sha on a copied local path must refuse")


# ----------------------------------------------------- cache TTL and ETag

def case_cache_ttl_and_etag(tmp):
    home = tmp / "home"
    url = "https://registry.example/paths.json"
    index_text = json.dumps({"schema": 1, "paths": []})
    calls = []

    def fetcher(u, etag):
        calls.append((u, etag))
        if etag == "v1":
            return None, "v1"  # 304 Not Modified
        return index_text, "v1"

    clock = [1000.0]
    kw = {"now": lambda: clock[0], "fetcher": fetcher}

    idx = registry.fetch_index(home, url, **kw)
    assert idx["paths"] == [] and len(calls) == 1
    assert calls[0] == (url, None)
    assert (home / registry.CACHE_FILE).exists()

    # Within the TTL: served from cache, the fetcher NEVER runs — the
    # one GET is also the only request the registry ever makes.
    clock[0] += registry.CACHE_TTL - 5
    registry.fetch_index(home, url, **kw)
    assert len(calls) == 1, "a fresh cache must not refetch"

    # Past the TTL: revalidate with the cached ETag; a 304 refreshes
    # the TTL without re-downloading.
    clock[0] += 10
    idx = registry.fetch_index(home, url, **kw)
    assert idx["paths"] == [] and len(calls) == 2
    assert calls[1] == (url, "v1"), "revalidation must send the ETag"
    clock[0] += 100
    registry.fetch_index(home, url, **kw)
    assert len(calls) == 2, "a 304 must refresh the cache TTL"

    # A different registry URL never serves the old cache.
    other = json.dumps({"schema": 1, "paths": [{"id": "a/b"}]})
    idx = registry.fetch_index(home, "https://other.example/paths.json",
                               now=kw["now"], fetcher=lambda u, e: (other, None))
    assert idx["paths"] == [{"id": "a/b"}]

    # force=True bypasses a fresh cache (used after tamper suspicion).
    registry.fetch_index(home, url, force=True, **kw)
    assert len(calls) == 3


def test_registry_index_parse_failures(tmp):
    home = tmp / "home"
    for text, expect in (("not json at all", "JSON"),
                         (json.dumps([1, 2]), "malformed"),
                         (json.dumps({"schema": 99, "paths": []}), "schema"),
                         (json.dumps({"schema": 1, "paths": [17]}),
                          "malformed")):
        try:
            registry.fetch_index(home, "https://x.example/paths.json",
                                 force=True, fetcher=lambda u, e: (text, None))
        except RegistryError as e:
            assert expect in str(e), (text, str(e))
        else:
            raise AssertionError(f"must refuse index {text!r}")
    p = tmp / "missing.json"
    try:
        registry.fetch_index(home, f"file://{p}", force=True)
    except RegistryError as e:
        assert "unreadable" in str(e)
    else:
        raise AssertionError("a missing file:// index must raise")


# ------------------------------------------------- author listing entries

def test_listing_entry_fields(tmp):
    course = _compile(tmp, tmp / "course")
    template = publish_course(course, tmp / "template", author="Test Er")
    entry = registry.listing_entry(template, derived_from="someone/earlier")
    assert entry["id"] == "test-er/survey-synthesis-for-practitioners"
    assert entry["name"] == "Survey Synthesis for Practitioners"
    assert registry.REPO_PREFIX in entry["git_url"]
    assert entry["commit_sha"] == "", \
        "pinning is the author's step — never invented"
    assert entry["license"] == "CC-BY-4.0"
    assert entry["assumed_knowledge"] == ["spreadsheets"]
    assert entry["est_hours"] > 0
    assert entry["derived_from"] == "someone/earlier"
    assert "verified" not in entry, "badge fields are CI-only"
    try:
        registry.listing_entry(tmp)
    except RegistryError as e:
        assert "course.yaml" in str(e)
    else:
        raise AssertionError("a non-template dir must refuse")


# ------------------------------------------- the public journey page (WS3c)

def _marker_fixtures(tmp: Path) -> Path:
    fdir = tmp / "fixtures"
    shutil.copytree(FIX, fdir)
    intake = json.loads((FIX / "intake.json").read_text())
    intake["constraints"] = {"weekly_hours": 5, "hardware": HW}
    (fdir / "intake.json").write_text(json.dumps(intake))
    return fdir


def _used_journey(tmp: Path) -> Path:
    home = _home(tmp)
    course = _compile(tmp, home / "courses" / "survey", topic=TOPIC,
                      fdir=_marker_fixtures(tmp), hardware=HW)
    (course / "00-data-audit" / "artifact.md").write_text(ARTIFACT)
    (course / "00-data-audit" / "reflection.md").write_text(REFLECTION)
    r = _grade_mock(course, "00-data-audit")
    assert r["passed"]
    return home


def case_journey_page_is_scrubbed(tmp):
    home = _used_journey(tmp)
    # The learner block really does carry the markers the page must drop.
    manifest = yaml.safe_load(
        (home / "courses" / "survey" / "course.yaml").read_text())
    assert manifest["learner"]["hardware"] == HW
    assert manifest["meta"]["topic_prompt"] == TOPIC

    dest = journey.publish_page(home, tmp / "page")
    files = sorted(p.name for p in dest.iterdir())
    assert files == sorted(journey.PAGE_FILES), \
        "the page is exactly two files — nothing else ships"

    text = "".join((dest / n).read_text() for n in journey.PAGE_FILES)
    # Concepts and grades are the POINT of the page — they stay.
    assert "sampling frame" in text and "non-response bias" in text
    assert "grade 100%" in text
    assert "Survey Synthesis for Practitioners" in text

    # Adversarial grep: zero learner data, zero local paths.
    for marker in ("TOPICPROMPT9", "HWSTRING7", "ARTSENT3", "REFLSENT5"):
        assert marker not in text, f"learner marker {marker} leaked"
    assert str(home) not in text and str(home.resolve()) not in text, \
        "journey-home paths must never reach the public page"
    assert "portfolio/claims" not in text, \
        "the public page carries no bundle filesystem links"

    meta, _ = okf.parse_doc(dest / "knowledge.md")
    assert meta["type"] == "journey" and meta["concept_count"] == 2
    html = (dest / "index.html").read_text()
    assert "<h1>" in html and "Knowledge map" in html


def case_leaky_page_is_refused(tmp):
    """If page content WOULD contain learner work (here: a course title
    that quotes the artifact), the gate refuses and writes nothing."""
    fdir = _marker_fixtures(tmp)
    intake = json.loads((fdir / "intake.json").read_text())
    intake["topic"] = ARTIFACT  # title now quotes the learner's artifact
    (fdir / "intake.json").write_text(json.dumps(intake))
    home = _home(tmp)
    course = _compile(tmp, home / "courses" / "survey", topic=TOPIC,
                      fdir=fdir, hardware=HW)
    (course / "00-data-audit" / "artifact.md").write_text(ARTIFACT)
    (course / "00-data-audit" / "reflection.md").write_text(REFLECTION)
    assert _grade_mock(course, "00-data-audit")["passed"]

    dest = tmp / "page"
    try:
        journey.publish_page(home, dest)
    except PublishError as e:
        assert "learner" in str(e) and "ARTSENT3" not in str(e), \
            "the refusal must not relay the leaked text"
    else:
        raise AssertionError("a leaking page must be refused")
    assert not dest.exists(), "a refused publish must write nothing"


def test_page_problems_gate_classes(tmp):
    home = _used_journey(tmp)
    staged = tmp / "staged"
    staged.mkdir()
    (staged / "knowledge.md").write_text(
        f"fine text\n{ARTIFACT}\nand {HW} plus {TOPIC}\nhome: {home}\n")
    problems = "\n".join(journey.page_problems(home, staged))
    for expect in ("learner work", "learner hardware",
                   "learner topic prompt", "journey home path"):
        assert expect in problems, f"gate must catch {expect}: {problems}"
    for marker in ("ARTSENT3", "HWSTRING7", "TOPICPROMPT9"):
        assert marker not in problems, "problems must not relay the leak"
    (staged / "knowledge.md").write_text("- **sampling frame** — grade 100%\n")
    assert journey.page_problems(home, staged) == [], \
        "concepts and grades are the page's purpose, not leaks"


# ------------------------------------------------------------- CLI wiring

def _cli(argv: list[str]) -> str:
    os.environ["SYLABIS_NO_UPDATE_CHECK"] = "1"
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli.main(argv)
    return buf.getvalue()


def test_paths_cli_search_get(tmp):
    home = _home(tmp)
    repo, sha = _template_repo(tmp)
    url = _index_url(tmp, [_entry(repo, sha)])

    out = _cli(["paths", "search", "survey", "--home", str(home),
                "--registry", url])
    assert "tester/survey-synthesis" in out and "CC-BY-4.0" in out
    out = _cli(["paths", "search", "zzz-none", "--home", str(home),
                "--registry", url])
    assert "No matching paths" in out

    out = _cli(["paths", "get", "tester/survey-synthesis",
                "--home", str(home), "--registry", url])
    assert "Attached" in out and sha[:12] in out
    assert journey.course_dirs(home), "get must land in the journey"
    assert (home / "knowledge.md").exists(), "get refreshes the map"


def test_publish_list_and_journey_publish_cli(tmp):
    course = _compile(tmp, tmp / "course")
    out = _cli(["publish", str(course), "--to", str(tmp / "template"),
                "--author", "tester", "--list"])
    assert '"id": "tester/survey-synthesis-for-practitioners"' in out
    assert '"commit_sha": ""' in out
    assert "sylabis-path-" in out and "rev-parse" in out, \
        "the pinning step is the author's, spelled out"
    assert "gh pr" not in out, "no gh dependency — manual PR steps only"

    home = _used_journey(tmp)
    out = _cli(["journey", "--publish", str(tmp / "page"),
                "--home", str(home)])
    assert "published" in out.lower()
    assert (tmp / "page" / "index.html").exists()
    assert (tmp / "page" / "knowledge.md").exists()
