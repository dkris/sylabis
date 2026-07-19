"""
WS1b — terminal surface: readline guard, verbatim multi-line paste submit,
crash-proof turns (retry + keep-the-session), transcript persistence and
trimming, and the lazily-imported Textual TUI. Fully offline: the model is
always a stub or mock fixtures; anthropic is imported only to construct
API-error-shaped exceptions, never a client.

Collected by tests/run_all.py (top-level case_*/test_* functions, optional
tmp Path arg). Self-contained on purpose — importing helpers from run_all
would re-execute it under `python -m tests.run_all`.
"""
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import anthropic
import httpx
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from sylabis import agent as agent_mod  # noqa: E402
from sylabis.agent import Agent, read_multiline, trim_messages  # noqa: E402
from sylabis.compiler import compile_course  # noqa: E402
from sylabis.console import Console  # noqa: E402
from sylabis.errors import SylabisError  # noqa: E402
from sylabis.llm import LLM  # noqa: E402

FIX = Path(__file__).parent.parent / "fixtures"


# ------------------------------------------------------------------ helpers

def _home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    return home


def _compile_into(home: Path, name: str = "survey") -> Path:
    out = home / "courses" / name
    with redirect_stdout(io.StringIO()):
        compile_course("Survey synthesis", {"weekly_hours": 5}, out,
                       LLM(mock=True, fixtures_dir=FIX))
    return out


def _feed(lines: list[str]):
    """An injectable stand-in for input(): pops lines, then EOF."""
    it = iter(lines)

    def fake_input(prompt: str = "") -> str:
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return fake_input


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


def _conn_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def _auth_error() -> anthropic.AuthenticationError:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError(
        "invalid x-api-key", response=httpx.Response(401, request=req),
        body=None)


class _FlakyModel:
    """LLM.chat stand-in: raises the queued exceptions first (one per
    call), then serves replies, streaming text like the real client."""

    def __init__(self, replies, failures=()):
        self.replies = list(replies)
        self.failures = list(failures)
        self.calls = 0

    def chat(self, system, messages, tools, on_text=None, **kw):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        resp = self.replies.pop(0)
        if on_text is not None:
            for b in resp.content:
                if b.type == "text":
                    on_text(b.text)
        return resp


def _agent(tmp: Path, llm) -> Agent:
    return Agent(_home(tmp), llm=llm, console=Console(enabled=False))


# ------------------------------------------------------- readline + paste

def test_readline_import_is_guarded():
    """agent.py must import whether or not readline exists; the module
    attribute is the guard seam (None on platforms without it)."""
    assert hasattr(agent_mod, "readline")
    # On this platform it should have resolved to the module or None —
    # either way, Agent import/construction never depends on it.


def test_read_multiline_dot_terminator_verbatim():
    """A 200-line paste reaches the caller byte-identical: trailing
    whitespace kept, blank lines kept, near-terminator lines ('..', ' .')
    kept, nothing trimmed but the lone '.' terminator itself."""
    lines = [f"line {i} — with trailing spaces  " for i in range(196)]
    lines += ["", "  .", "..", "\tfinal line\t"]
    assert len(lines) == 200
    text = read_multiline(_feed(lines + ["."]))
    assert text == "\n".join(lines), "paste must be byte-identical"
    assert text.endswith("\tfinal line\t")


def test_read_multiline_eof_terminator():
    lines = ["alpha", "beta  "]
    assert read_multiline(_feed(lines)) == "alpha\nbeta  ", \
        "Ctrl-D (EOF) must terminate and keep everything typed"
    assert read_multiline(_feed([])) == ""


def test_slash_submit_paste_submits_verbatim(tmp):
    """/submit captures artifact + reflection through paste mode and hands
    them to submit_work untouched — the written files are byte-identical
    to what the learner typed, and the grade comes from the tool."""
    home = _home(tmp)
    course = _compile_into(home)
    ag = Agent(home, mock=True, console=Console(enabled=False))
    artifact_lines = [f"observation {i}: 61% of respondents (n=140)  "
                      for i in range(200)]
    reflection_lines = ["I wanted the data to support more than it can;",
                        "the skew is a generalization boundary."]
    ag._input = _feed(artifact_lines + ["."] + reflection_lines + ["."])
    with redirect_stdout(io.StringIO()):
        ag._command("/submit", [])
    written = (course / "00-data-audit" / "artifact.md").read_text()
    assert written == "\n".join(artifact_lines), \
        "artifact must reach submit_work byte-identical"
    assert (course / "00-data-audit" / "reflection.md").read_text() \
        == "\n".join(reflection_lines)
    assert (course / "00-data-audit" / "grade.yaml").exists(), \
        "the grade must come from submit_work, nowhere else"


def test_slash_submit_reflection_only_milestone(tmp):
    """A milestone whose checkpoint requires only reflection.md prompts
    for the reflection alone — the learner is never asked for an
    artifact that the checkpoint does not require."""
    home = _home(tmp)
    course = _compile_into(home)
    cp_path = course / "00-data-audit" / "checkpoint.yaml"
    cp = yaml.safe_load(cp_path.read_text())
    cp["structural"]["required_files"] = ["reflection.md"]
    cp_path.write_text(yaml.dump(cp))
    ag = Agent(home, mock=True, console=Console(enabled=False))
    ag._input = _feed(["boundary: findings do not extend to SMB", "."])
    with redirect_stdout(io.StringIO()):
        ag._command("/submit", [])
    assert (course / "00-data-audit" / "reflection.md").read_text() \
        == "boundary: findings do not extend to SMB"


def test_slash_submit_empty_journey_is_a_notice(tmp):
    ag = Agent(_home(tmp), mock=True, console=Console(enabled=False))
    ag._input = _feed([])
    ag._command("/submit", [])  # must not crash or prompt


# ------------------------------------------------------- crash-proof turns

def test_turn_retries_api_error_then_recovers(tmp):
    """API error on the first model call of a turn -> backoff retry ->
    recovery; the transcript comes out exactly as if nothing failed."""
    replies = [
        _Resp([_Block(type="tool_use", name="journey", input={}, id="t1")],
              "tool_use"),
        _Resp([_Block(type="text", text="Recovered.")], "end_turn"),
    ]
    llm = _FlakyModel(replies, failures=[_conn_error()])
    ag = _agent(tmp, llm)
    ag.retry_base = 0  # no real sleeping in the suite
    messages = [{"role": "user", "content": "hi"}]
    reply = ag.turn(messages)
    assert reply == "Recovered."
    assert llm.calls == 3, "1 failure + 2 successful rounds"
    assert not llm.replies and len(messages) == 4, "full turn transcript"
    assert messages[1]["content"][0]["type"] == "tool_use"
    assert messages[2]["content"][0]["type"] == "tool_result"


def test_turn_final_failure_keeps_session_alive(tmp):
    """Simulated network failure mid-turn, past all retries: the turn ends
    with an error, but the transcript stays intact and well-formed and the
    next turn works — the session is never lost."""
    replies = [
        _Resp([_Block(type="tool_use", name="journey", input={}, id="t1")],
              "tool_use"),
        _Resp([_Block(type="text", text="Back online.")], "end_turn"),
    ]
    llm = _FlakyModel(replies)
    orig_chat = llm.chat

    def flaky_chat(system, messages, tools, on_text=None, **kw):
        # round 1 (tool_use) succeeds; every call after that fails until
        # the failure queue drains
        if llm.calls >= 1 and flaky_chat.down:
            llm.calls += 1
            flaky_chat.down -= 1
            raise _conn_error()
        return orig_chat(system, messages, tools, on_text=on_text, **kw)
    flaky_chat.down = 3  # exactly the retry budget
    llm.chat = flaky_chat

    ag = _agent(tmp, llm)
    ag.retry_base = 0
    messages = [{"role": "user", "content": "hi"}]
    reply = ag.turn(messages)
    assert reply == "", "no prose was produced before the failure"
    assert len(messages) == 3, "user + tool_use + tool_result kept intact"
    assert messages[1]["content"][0]["type"] == "tool_use"
    assert messages[2]["content"][0]["type"] == "tool_result", \
        "no dangling tool_use — the transcript must stay well-formed"
    assert llm.calls == 4, "1 success + 3 exhausted retries"
    # the session is alive: the very next turn completes normally
    messages.append({"role": "user", "content": "still there?"})
    assert ag.turn(messages) == "Back online."


def test_turn_does_not_retry_auth_errors(tmp):
    """A 401 is not transient — one attempt, fail fast, session kept."""
    llm = _FlakyModel([], failures=[_auth_error(), _auth_error(),
                                    _auth_error()])
    ag = _agent(tmp, llm)
    ag.retry_base = 0
    messages = [{"role": "user", "content": "hi"}]
    assert ag.turn(messages) == ""
    assert llm.calls == 1, "auth errors must not be retried"
    assert messages == [{"role": "user", "content": "hi"}]


# --------------------------------------------------- persistence + trimming

def test_session_persist_and_resume(tmp):
    home = _home(tmp)
    ag = Agent(home, mock=True, console=Console(enabled=False))
    messages = [{"role": "user", "content": "hi"},
                {"role": "assistant",
                 "content": [{"type": "text", "text": "hello"}]}]
    ag.save_session(messages)
    assert (home / ".session.json").exists()
    fresh = Agent(home, mock=True, console=Console(enabled=False))
    assert fresh.load_session() == messages, \
        "a fresh `sy` must resume the conversation"
    data = json.loads((home / ".session.json").read_text())
    assert data["schema"] == 1


def test_session_corrupt_file_starts_clean(tmp):
    home = _home(tmp)
    (home / ".session.json").write_text("{not json")
    ag = Agent(home, mock=True, console=Console(enabled=False))
    assert ag.load_session() == []
    (home / ".session.json").write_text('{"messages": "nope"}')
    assert ag.load_session() == []


def test_clear_command_deletes_session(tmp):
    home = _home(tmp)
    ag = Agent(home, mock=True, console=Console(enabled=False))
    messages = [{"role": "user", "content": "hi"}]
    ag.save_session(messages)
    ag._command("/clear", messages)
    assert messages == [] and not (home / ".session.json").exists()


def test_trim_drops_oldest_tool_results_first():
    tool_pair = lambda i: [  # noqa: E731
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "journey", "input": {},
             "id": f"t{i}"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{i}",
             "content": "x", "is_error": False}]}]
    messages = ([{"role": "user", "content": "q1"}] + tool_pair(1)
                + tool_pair(2)
                + [{"role": "assistant",
                    "content": [{"type": "text", "text": "a1"}]},
                   {"role": "user", "content": "q2"},
                   {"role": "assistant",
                    "content": [{"type": "text", "text": "a2"}]}])
    trim_messages(messages, budget=6)
    assert len(messages) <= 6
    # the oldest tool exchange went first; prose survived
    assert messages[0] == {"role": "user", "content": "q1"}
    ids = [b["id"] for m in messages if isinstance(m["content"], list)
           for b in m["content"] if b.get("type") == "tool_use"]
    assert ids == ["t2"], "oldest tool exchange dropped first"
    # no dangling tool_use anywhere
    for i, m in enumerate(messages):
        if agent_mod._is_tool_use_msg(m):
            assert agent_mod._is_tool_result_msg(messages[i + 1])


def test_trim_falls_back_to_oldest_turns_and_keeps_user_first():
    messages = []
    for i in range(6):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": f"a{i}"}]})
    trim_messages(messages, budget=4)
    assert len(messages) <= 4
    assert messages[0]["role"] == "user", \
        "the transcript must always start with a user message"
    assert messages[-1]["content"][0]["text"] == "a5", "newest turns kept"


def test_turn_trims_before_calling_the_model(tmp):
    """A transcript over budget gets trimmed inside turn() so the rollback
    baseline is the trimmed transcript, not the bloated one."""
    llm = _FlakyModel([_Resp([_Block(type="text", text="ok")], "end_turn")])
    ag = _agent(tmp, llm)
    messages = []
    for i in range(agent_mod.MESSAGE_BUDGET + 10):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant",
                         "content": [{"type": "text", "text": f"a{i}"}]})
    messages.append({"role": "user", "content": "latest"})
    ag.turn(messages)
    assert len(messages) <= agent_mod.MESSAGE_BUDGET + 1, \
        "trim runs before the model call"
    assert messages[-1]["content"][0]["text"] == "ok"


# ------------------------------------------------------------------- TUI

def test_tui_module_imports_without_textual():
    """sylabis.tui must import (and be honest about availability) whether
    or not the optional Textual extra is installed."""
    import sylabis.tui as tui
    assert callable(tui.run_tui) and callable(tui.tui_available)
    assert isinstance(tui.tui_available(), bool)
    if tui.tui_available():
        return  # textual installed here — the unavailable path is moot
    try:
        tui.run_tui(Path("."))
    except SylabisError as e:
        assert "sylabis[tui]" in str(e)
    else:
        raise AssertionError("run_tui without textual must raise "
                             "SylabisError, never crash or half-start")


def test_tui_console_ducktype_smokes(tmp):
    """The TUI console adapter drives the same trace interface as
    console.Console — exercised headlessly. _build_console_class needs
    only sylabis.console, not textual, so this runs everywhere."""
    import sylabis.tui as tui

    class _FakeApp:
        def __init__(self):
            self.lines, self.status = [], []

        def post_line(self, line):
            self.lines.append(line)

        def post_status(self, s):
            self.status.append(s)

    TuiConsole = tui._build_console_class()
    app = _FakeApp()
    ui = TuiConsole(app)
    assert ui.enabled and not ui.color
    ui.header("t", ["sub"])
    ui.text_delta("hel")
    ui.text_delta("lo\nwor")
    ui.turn_end()  # flushes the partial line
    ui.tool_call("submit_work", {"artifact": "x" * 500, "course": "survey"})
    ui.tool_result("line1\nline2")
    ui.spinner("grading")
    ui.stop_spinner()
    joined = "\n".join(app.lines)
    assert "hello" in joined and "wor" in joined
    assert "(500 chars)" in joined and "x" * 60 not in joined, \
        "collapse-long-args behavior must carry over from console.py"
    assert app.status == ["grading…", ""]


def test_tui_full_app_smoke(tmp):
    """Only when textual happens to be installed: the app class builds.
    Never a required dependency of the suite — skip (return) without it."""
    import sylabis.tui as tui
    if not tui.tui_available():
        return  # textual not installed — optional extra, skip by contract
    home = _home(tmp)
    app_cls = tui._build_app_class()
    app = app_cls(home, mock=True)
    assert app.messages == [] and app.agent.home == home
