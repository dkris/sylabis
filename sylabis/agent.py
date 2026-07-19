"""
The agent harness. `sylabis` with no arguments lands here: a conversation
in the terminal where the model drives the whole learn → submit → grade →
adapt loop through the journey tools. The harness stays thin — everything
that matters lives in the tools (tools.py) and the guide prompt
(prompts.py) — but the *experience* is a modern agent CLI: prose streams
as it generates, every tool call renders as a trace line with a result
preview, a spinner covers the silences, slash commands handle the
mechanical stuff locally, and Ctrl-C abandons a turn without losing the
session. The same Agent runs silently under the web harness
(console disabled), so there is exactly one conversation loop.

Reliability (Primetime WS1b): line editing + history through readline
(guarded — absent on some platforms), a verbatim multi-line paste mode
for /submit, retry-with-backoff around every model call so a dropped
connection never kills the session, and transcript persistence to
$SYLABIS_HOME/.session.json so a fresh `sy` resumes the conversation.
"""
import json
import sys
import time
from pathlib import Path

import yaml

from . import journey
from .console import Console
from .errors import ModelError
from .llm import LLM
from .prompts import GUIDE_SYSTEM
from .tools import JourneyTools, ToolError

try:  # guarded: readline is absent on some platforms (native Windows)
    import readline
except ImportError:  # pragma: no cover - platform dependent
    readline = None

MAX_TOOL_ROUNDS = 12   # per user turn; a guide that needs more is looping
MESSAGE_BUDGET = 80    # transcript messages kept; oldest tool results go first
RETRY_ATTEMPTS = 3     # .chat() retries live here (LLM.call retries in llm.py)
SESSION_FILE = ".session.json"
HISTORY_FILE = ".history"
PASTE_TERMINATOR = "."

_HELP = """\
/help      these commands
/journey   where you are — courses, progress, knowledge (no model call)
/next      the single next action (no model call)
/submit    paste your artifact/reflection verbatim (end with a lone '.' or Ctrl-D)
/clear     start a fresh conversation (your journey is untouched)
/quit      leave (Ctrl-D works too)
Ctrl-C     interrupt the current turn; nothing from it is kept
↑ / ↓      input history (when readline is available)"""


def read_multiline(input_fn) -> str:
    """Capture learner text verbatim: read lines until a lone '.' line or
    EOF (Ctrl-D). Input capture only — the text is never transformed,
    trimmed, or completed; what the learner typed is exactly what gets
    submitted. (A content line that is exactly '.' can't be pasted this
    way — end with Ctrl-D instead.)"""
    lines: list[str] = []
    while True:
        try:
            line = input_fn()
        except EOFError:
            break
        if line == PASTE_TERMINATOR:
            break
        lines.append(line)
    return "\n".join(lines)


def _is_tool_result_msg(m: dict) -> bool:
    c = m.get("content")
    return (m.get("role") == "user" and isinstance(c, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in c))


def _is_tool_use_msg(m: dict) -> bool:
    c = m.get("content")
    return (m.get("role") == "assistant" and isinstance(c, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in c))


def trim_messages(messages: list[dict], budget: int = MESSAGE_BUDGET) -> None:
    """Trim the transcript in place to the message budget, dropping the
    oldest tool exchanges (tool_use + its tool_result — always as a pair,
    a dangling tool_use poisons every later call) before touching prose.
    The result always starts with a plain user message."""
    while len(messages) > budget:
        idx = next((i for i in range(len(messages) - 1)
                    if _is_tool_use_msg(messages[i])
                    and _is_tool_result_msg(messages[i + 1])), None)
        if idx is not None:
            del messages[idx:idx + 2]
            continue
        del messages[0]
        while messages and (messages[0].get("role") != "user"
                            or _is_tool_result_msg(messages[0])):
            del messages[0]


_RETRYABLE: tuple | None = None


def _retryable_errors() -> tuple:
    """Exception types worth a retry. anthropic imports lazily so the
    mock/offline paths (and platforms without the SDK) still work."""
    global _RETRYABLE
    if _RETRYABLE is None:
        errs: list[type] = [ModelError]
        try:
            import anthropic
            errs.append(anthropic.APIError)
        except Exception:  # pragma: no cover - SDK always present in prod
            pass
        _RETRYABLE = tuple(errs)
    return _RETRYABLE


def _worth_retrying(e: Exception) -> bool:
    """Rate limits, overload, and network drops are transient; auth and
    malformed-request errors are not — fail those fast."""
    status = getattr(e, "status_code", None)
    if status is None:  # connection/timeout errors carry no status
        return True
    return status in (408, 409, 429) or status >= 500


class Agent:
    def __init__(self, home_dir: Path, mock: bool = False,
                 llm: LLM | None = None, console: Console | None = None):
        self.home = Path(home_dir)
        self.tools = JourneyTools(self.home, mock=mock)
        self.llm = llm or LLM(mock=mock)
        self.ui = console if console is not None else Console()
        self.specs = [{"name": n, "description": d, "input_schema": s}
                      for n, (_, d, s) in self.tools.registry.items()]
        self.retry_attempts = RETRY_ATTEMPTS
        self.retry_base = 1.0     # seconds; doubles per attempt
        self._input = input       # injectable for tests — never AI-mediated

    # ---------------------------------------------------------------- REPL

    def run(self) -> None:
        self._init_readline()
        messages = self.load_session()
        first = not messages
        self.ui.header("sylabis — the learning agent", self._status_lines()
                       + ["/help for commands · Ctrl-D to leave"])
        if messages:
            self.ui.notice(f"resumed your last conversation "
                           f"({len(messages)} messages) — /clear starts fresh")
        try:
            while True:
                try:
                    user = self._input(self._prompt()).strip()
                except EOFError:
                    self.ui.turn_end()
                    return
                except KeyboardInterrupt:
                    print()
                    self.ui.notice("(Ctrl-D or /quit to leave)")
                    continue
                if not user:
                    continue
                if user.startswith("/"):
                    if self._command(user, messages):
                        return
                    continue
                if first:
                    # First turn carries the standing instruction to orient.
                    user = f"(new session — orient first)\n{user}"
                    first = False
                messages.append({"role": "user", "content": user})
                self.turn(messages)
                self.save_session(messages)
                self.ui.turn_end()
        finally:
            self._save_history()

    def _prompt(self) -> str:
        return self.ui.cyan("\n❯ ") if self.ui.color else "\n❯ "

    def _status_lines(self) -> list[str]:
        steps = journey.next_steps(self.home)
        if not steps:
            return ["your journey is empty — say what you want to learn"]
        know = len(journey.knowledge(self.home))
        line = (f"{len(steps)} course{'s' if len(steps) != 1 else ''} · "
                f"{know} verified concept{'s' if know != 1 else ''}")
        ready = [s for s in steps if s["status"] == "ready"]
        if ready:
            s = ready[0]
            line += f" · next: {s['milestone_id']} — {s['title']}"
        return [line]

    # ----------------------------------------------------------- readline

    def _init_readline(self) -> None:
        if readline is None:
            return
        try:
            hist = self.home / HISTORY_FILE
            if hist.exists():
                readline.read_history_file(str(hist))
            readline.set_history_length(1000)
        except OSError:
            pass  # history is a convenience, never a crash

    def _save_history(self) -> None:
        if readline is None:
            return
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(str(self.home / HISTORY_FILE))
        except OSError:
            pass

    # -------------------------------------------------------- persistence

    def _session_file(self) -> Path:
        return self.home / SESSION_FILE

    def save_session(self, messages: list[dict]) -> None:
        """Best-effort transcript persistence — a fresh `sy` resumes the
        conversation. Never kills the session over a disk problem."""
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            self._session_file().write_text(json.dumps(
                {"schema": 1, "messages": messages}, ensure_ascii=False))
        except (OSError, TypeError, ValueError):
            pass

    def load_session(self) -> list[dict]:
        try:
            data = json.loads(self._session_file().read_text())
        except (OSError, ValueError):
            return []
        msgs = data.get("messages") if isinstance(data, dict) else None
        return msgs if isinstance(msgs, list) else []

    # ------------------------------------------------------ slash commands

    def _command(self, cmd: str, messages: list[dict]) -> bool:
        """Local slash commands — mechanical questions get instant answers
        from the tools, no model round-trip. Returns True to exit."""
        parts = cmd.split(maxsplit=1)
        name = parts[0].lower()
        if name in ("/quit", "/exit"):
            return True
        if name == "/help":
            self.ui.text_block("\n" + _HELP)
        elif name == "/clear":
            messages.clear()
            self._session_file().unlink(missing_ok=True)
            self.ui.notice("conversation cleared — the journey is untouched")
        elif name in ("/journey", "/next"):
            tool = "journey" if name == "/journey" else "next_step"
            self.ui.tool_call(tool, {})
            try:
                self.ui.tool_result(self.tools.call(tool, {}))
            except ToolError as e:
                self.ui.tool_result(str(e), error=True)
        elif name == "/submit":
            self._submit_paste(parts[1].strip() if len(parts) > 1 else None)
        else:
            self.ui.notice(f"unknown command {name} — /help lists them")
        return False

    def _submit_paste(self, which: str | None = None) -> None:
        """Multi-line paste mode for the core loop: capture the learner's
        artifact/reflection VERBATIM (lone '.' line or Ctrl-D ends each),
        then hand it straight to submit_work. The guide never sees, edits,
        or completes this text — and the grade comes only from the tool."""
        candidates = journey.submittable(self.home)
        if which:
            candidates = [c for c in candidates
                          if c["milestone_id"] == which or c["course"] == which]
        if not candidates:
            self.ui.notice("nothing to submit against — /next shows where "
                           "you are")
            return
        if len(candidates) > 1:
            self.ui.notice("several milestones can take work — pick one:")
            for c in candidates:
                self.ui.notice(f"  /submit {c['milestone_id']}   "
                               f"({c['course']})")
            return
        step = candidates[0]
        cdir = journey.course_dir(self.home, step["course"])
        cp_path = cdir / step["milestone_id"] / "checkpoint.yaml"
        cp = yaml.safe_load(cp_path.read_text()) or {}
        required = (cp.get("structural") or {}).get("required_files") \
            or ["artifact.md", "reflection.md"]

        texts = {"artifact": "", "reflection": ""}
        read_line = lambda: self._input("")  # noqa: E731 - injectable seam
        for field, fname in (("artifact", "artifact.md"),
                             ("reflection", "reflection.md")):
            if fname not in required:
                continue
            self.ui.notice(f"paste your {field} for {step['milestone_id']} — "
                           f"end with a lone '.' line or Ctrl-D")
            texts[field] = read_multiline(read_line)
        args = {"course": step["course"],
                "milestone_id": step["milestone_id"],
                "artifact": texts["artifact"],
                "reflection": texts["reflection"]}
        self.ui.tool_call("submit_work", args)
        self.ui.spinner("grading")
        try:
            result = self.tools.call("submit_work", args)
            self.ui.stop_spinner()
            self.ui.tool_result(result)
        except ToolError as e:
            self.ui.stop_spinner()
            self.ui.tool_result(str(e), error=True)

    # ---------------------------------------------------------------- turns

    def turn(self, messages: list[dict]) -> str:
        """One user turn: stream the model, run any tools it asks for, feed
        results back, repeat until it answers in text. Renders through the
        console as it goes and returns the full reply, so a silent harness
        (web chat) gets the same conversation without the terminal. Ctrl-C
        rolls the whole turn back — the transcript never holds a dangling
        tool call. A model call that fails after retries ends the turn but
        KEEPS the session: the transcript stays intact and well-formed
        (the failure happens before anything dangling is appended)."""
        trim_messages(messages)
        base = len(messages)
        said: list[str] = []
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                self.ui.turn_start()
                self.ui.spinner("thinking")
                streamed = False

                def on_text(delta: str) -> None:
                    nonlocal streamed
                    streamed = True
                    self.ui.text_delta(delta)

                try:
                    resp = self._chat_with_retry(
                        messages, on_text if self.ui.enabled else None)
                except ModelError as e:
                    self.ui.stop_spinner()
                    self.ui.error(str(e))
                    self.ui.notice("the session is intact — nothing was "
                                   "lost; say something to continue")
                    return "\n\n".join(said)
                self.ui.stop_spinner()
                content = []
                for b in resp.content:
                    d = b.model_dump()
                    for extra in getattr(b, "__api_exclude__", ()):
                        d.pop(extra, None)
                    content.append(d)
                messages.append({"role": "assistant", "content": content})
                texts = [b.text.strip() for b in resp.content
                         if b.type == "text" and b.text.strip()]
                said += texts
                if not streamed:
                    for t in texts:
                        self.ui.text_block(t)
                elif texts:
                    self.ui.text_delta("\n")
                if resp.stop_reason != "tool_use":
                    return "\n\n".join(said)
                results = []
                for block in resp.content:
                    if block.type != "tool_use":
                        continue
                    self.ui.tool_call(block.name, block.input)
                    self.ui.spinner(block.name)
                    result = self._run_tool(block)
                    self.ui.stop_spinner()
                    self.ui.tool_result(result["content"],
                                        error=result["is_error"])
                    results.append(result)
                messages.append({"role": "user", "content": results})
            self.ui.error("stopped — too many tool rounds in one turn")
            return "\n\n".join(said + ["(stopped — too many tool rounds "
                                       "in one turn)"])
        except KeyboardInterrupt:
            del messages[base:]
            self.ui.stop_spinner()
            print(file=sys.stderr)
            self.ui.notice("interrupted — this turn was rolled back")
            return ""

    def _chat_with_retry(self, messages: list[dict], on_text):
        """One model call, retried with exponential backoff on transient
        API errors. Raises ModelError when retries are exhausted (or the
        error isn't worth retrying) — the caller keeps the session alive."""
        delay = self.retry_base
        for attempt in range(1, self.retry_attempts + 1):
            try:
                return self.llm.chat(GUIDE_SYSTEM, messages, self.specs,
                                     on_text=on_text)
            except KeyboardInterrupt:
                raise
            except _retryable_errors() as e:
                if attempt == self.retry_attempts or not _worth_retrying(e):
                    raise ModelError(
                        f"model call failed after {attempt} attempt"
                        f"{'s' if attempt != 1 else ''}: "
                        f"{type(e).__name__}: {e}") from e
                self.ui.stop_spinner()
                self.ui.notice(f"model call failed ({type(e).__name__}) — "
                               f"retrying in {delay:.0f}s "
                               f"({attempt}/{self.retry_attempts - 1})")
                time.sleep(delay)
                delay = delay * 2 if delay else self.retry_base
                self.ui.spinner("thinking")

    def _run_tool(self, block) -> dict:
        try:
            text, is_error = self.tools.call(block.name, block.input), False
        except ToolError as e:
            text, is_error = str(e), True
        except KeyboardInterrupt:
            raise
        except Exception as e:  # a tool bug is self-correctable, not fatal
            text, is_error = f"Tool error: {e}", True
        return {"type": "tool_result", "tool_use_id": block.id,
                "content": text, "is_error": is_error}
