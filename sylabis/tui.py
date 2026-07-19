"""
Full-screen terminal app (Primetime WS1b) — a Textual *view* over the very
same Agent.turn() loop and tools.py registry the bare-ANSI harness drives.
Nothing about the conversation changes: the agent, the tools, the grades,
the transcript persistence all live in agent.py; this module only paints.

Three panes:
  - conversation: streaming prose + tool-trace lines (same collapse-long-
    args previews as console.py, imported from there);
  - journey sidebar: fed by the `journey`/`next_step` tools — no model
    calls, refreshed after every turn;
  - submit editor: INPUT CAPTURE ONLY. The learner's artifact/reflection
    text goes to submit_work byte-for-byte; there is no AI-assist widget
    in this screen and there never will be (product invariant: the guide
    never writes the learner's work).

Textual is an OPTIONAL extra (`pip install sylabis[tui]`). This module
must import cleanly without it — all textual imports live inside
functions; callers check tui_available() and fall back to Agent.run().
"""
from pathlib import Path

from .errors import SylabisError

TUI_HINT = ("the full-screen TUI needs Textual — install it with "
            "`pip install 'sylabis[tui]'`; plain `sy` works without it")


def tui_available() -> bool:
    """Cheap check: can the Textual app run here? (import only)."""
    try:
        import textual  # noqa: F401
        return True
    except ImportError:
        return False


def run_tui(home_dir: Path | str, mock: bool = False) -> None:
    """Launch the full-screen app over the journey at home_dir. Raises
    SylabisError when Textual is not installed — callers should prefer
    checking tui_available() first and falling back to Agent.run()."""
    if not tui_available():
        raise SylabisError(TUI_HINT)
    app_cls = _build_app_class()
    app_cls(Path(home_dir), mock=mock).run()


# ---------------------------------------------------------------- internals
# Everything below imports textual, so it is only reached behind
# tui_available(); module import never needs the dependency.

def _build_console_class():
    """A Console duck-type that routes the agent's trace into the app's
    conversation pane. Agent.turn() runs in a worker thread, so every
    write hops to the UI thread via call_from_thread."""
    from .console import preview_args, preview_result

    class TuiConsole:
        enabled = True
        color = False  # the pane does its own styling; no ANSI codes

        def __init__(self, app):
            self.app = app
            self._buf = ""  # partial streamed line

        # -- painting helpers (identity: Textual styles, not ANSI) --
        def dim(self, s):    return s
        def bold(self, s):   return s
        def cyan(self, s):   return s
        def red(self, s):    return s
        def green(self, s):  return s

        def _post(self, line: str) -> None:
            self.app.post_line(line)

        def _flush(self) -> None:
            if self._buf:
                self._post(self._buf)
                self._buf = ""

        # -- the Console interface agent.py drives --
        def header(self, title: str, lines: list) -> None:
            self._post(f"◆ {title}")
            for line in lines:
                self._post(f"  {line}")

        def text_delta(self, s: str) -> None:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._post(line)

        def text_block(self, s: str) -> None:
            self._flush()
            for line in s.splitlines() or [""]:
                self._post(line)

        def turn_start(self) -> None:
            self._flush()
            self._post("")

        def turn_end(self) -> None:
            self._flush()

        def tool_call(self, name: str, args: dict) -> None:
            self._flush()
            self._post(f"⏺ {name}({preview_args(args)})")

        def tool_result(self, text: str, error: bool = False) -> None:
            lines = preview_result(text)
            mark = "✗" if error else "⎿"
            self._post(f"  {mark}  {lines[0]}")
            for line in lines[1:]:
                self._post(f"      {line}")

        def notice(self, s: str) -> None:
            self._flush()
            self._post(f"  {s}")

        def error(self, s: str) -> None:
            self._flush()
            self._post(f"  ! {s}")

        def spinner(self, label: str) -> None:
            self.app.post_status(f"{label}…")

        def stop_spinner(self) -> None:
            self.app.post_status("")

    return TuiConsole


def _build_submit_screen():
    """The submit editor: milestone picker + two plain TextAreas. Input
    capture only — the text is dismissed verbatim; no completion, no
    rewrite, no AI-assist widget, ever."""
    from textual.app import ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import Button, Label, Select, TextArea

    class SubmitScreen(ModalScreen):
        BINDINGS = [("escape", "cancel", "Cancel")]

        def __init__(self, steps: list[dict]):
            super().__init__()
            self.steps = steps

        def compose(self) -> ComposeResult:
            options = [(f"{s['course']} / {s['milestone_id']} — {s['title']}",
                        i) for i, s in enumerate(self.steps)]
            with Vertical(id="submit-box"):
                yield Label("Submit your work — it is graded exactly as "
                            "written; nothing edits it for you.")
                yield Select(options, value=0, id="submit-milestone",
                             allow_blank=False)
                yield Label("Artifact")
                yield TextArea(id="submit-artifact")
                yield Label("Reflection")
                yield TextArea(id="submit-reflection")
                with Horizontal():
                    yield Button("Submit", id="submit-go", variant="primary")
                    yield Button("Cancel", id="submit-cancel")

        def on_button_pressed(self, event) -> None:
            if event.button.id == "submit-cancel":
                self.dismiss(None)
                return
            idx = self.query_one("#submit-milestone", Select).value
            step = self.steps[int(idx)]
            # VERBATIM: .text straight off the widgets, untouched.
            self.dismiss({
                "course": step["course"],
                "milestone_id": step["milestone_id"],
                "artifact": self.query_one("#submit-artifact", TextArea).text,
                "reflection": self.query_one("#submit-reflection",
                                             TextArea).text,
            })

        def action_cancel(self) -> None:
            self.dismiss(None)

    return SubmitScreen


def _build_app_class():
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.widgets import Footer, Input, Static

    from . import journey
    from .agent import Agent
    from .tools import ToolError

    TuiConsole = _build_console_class()
    SubmitScreen = _build_submit_screen()

    class SyApp(App):
        TITLE = "sylabis — the learning agent"
        BINDINGS = [
            ("ctrl+s", "open_submit", "Submit work"),
            ("ctrl+l", "clear_conversation", "Clear"),
            ("ctrl+q", "quit", "Quit"),
        ]
        CSS = """
        #main { width: 3fr; }
        #sidebar { width: 1fr; min-width: 24; border-left: solid $accent; padding: 0 1; }
        #conversation { padding: 0 1; }
        #status { height: 1; color: $text-muted; padding: 0 1; }
        #submit-box { width: 90%; height: 90%; border: solid $accent; padding: 1; background: $surface; }
        #submit-artifact { height: 1fr; }
        #submit-reflection { height: 1fr; }
        """

        def __init__(self, home_dir: Path, mock: bool = False):
            super().__init__()
            self.home = Path(home_dir)
            self.agent = Agent(self.home, mock=mock,
                               console=TuiConsole(self))
            self.messages: list[dict] = self.agent.load_session()
            self._first = not self.messages
            self._log_lines: list[str] = []  # conversation pane content

        # ---- layout ----

        def compose(self) -> ComposeResult:
            with Horizontal():
                with Vertical(id="main"):
                    yield VerticalScroll(Static("", id="log"),
                                         id="conversation")
                    yield Static("", id="status")
                    yield Input(placeholder="say what you want to learn — "
                                            "/help for commands",
                                id="prompt")
                yield Static("", id="sidebar")
            yield Footer()

        def on_mount(self) -> None:
            ui = self.agent.ui
            ui.header(self.TITLE, self.agent._status_lines()
                      + ["/help for commands · ctrl+s to submit work"])
            if self.messages:
                ui.notice(f"resumed your last conversation "
                          f"({len(self.messages)} messages)")
            self.refresh_sidebar()
            self.query_one("#prompt", Input).focus()

        # ---- pane plumbing (UI-thread only via post_*) ----

        def _append_line(self, line: str) -> None:
            self._log_lines.append(line)
            self.query_one("#log", Static).update("\n".join(self._log_lines))
            self.query_one("#conversation", VerticalScroll).scroll_end(
                animate=False)

        def _set_status(self, s: str) -> None:
            self.query_one("#status", Static).update(s)

        def post_line(self, line: str) -> None:
            self._from_any_thread(self._append_line, line)

        def post_status(self, s: str) -> None:
            self._from_any_thread(self._set_status, s)

        def _from_any_thread(self, fn, *args) -> None:
            try:
                self.call_from_thread(fn, *args)
            except RuntimeError:  # already on the app thread
                fn(*args)

        # ---- journey sidebar: tools only, never a model call ----

        def refresh_sidebar(self) -> None:
            def render() -> str:
                try:
                    overview = self.agent.tools.call("journey", {})
                    nxt = self.agent.tools.call("next_step", {})
                except ToolError as e:
                    return f"journey unavailable:\n{e}"
                return f"JOURNEY\n\n{overview}\n\nNEXT\n\n{nxt}"

            def work() -> None:
                text = render()
                self._from_any_thread(
                    self.query_one("#sidebar", Static).update, text)

            self.run_worker(work, thread=True, exclusive=False)

        # ---- input ----

        def on_input_submitted(self, event) -> None:
            text = event.value.strip()
            event.input.value = ""
            if not text:
                return
            if text.startswith("/"):
                self._slash(text)
                return
            self.agent.ui.notice(f"❯ {text}")
            if self._first:
                text = f"(new session — orient first)\n{text}"
                self._first = False
            self.messages.append({"role": "user", "content": text})

            def work() -> None:
                self.agent.turn(self.messages)
                self.agent.save_session(self.messages)
                self.agent.ui.turn_end()
                self._from_any_thread(self.refresh_sidebar)

            self.run_worker(work, thread=True, exclusive=True)

        def _slash(self, cmd: str) -> None:
            name = cmd.split()[0].lower()
            if name in ("/quit", "/exit"):
                self.exit()
                return
            if name == "/submit":
                self.action_open_submit()
                return
            if name == "/clear":
                self.action_clear_conversation()
                return

            def work() -> None:
                # /help /journey /next render through the same console
                self.agent._command(cmd, self.messages)

            self.run_worker(work, thread=True, exclusive=True)

        def action_clear_conversation(self) -> None:
            self.messages.clear()
            self.agent._session_file().unlink(missing_ok=True)
            self._first = True
            self._log_lines.clear()
            self.query_one("#log", Static).update("")
            self.agent.ui.notice("conversation cleared — the journey is "
                                 "untouched")

        # ---- submit editor ----

        def action_open_submit(self) -> None:
            steps = journey.submittable(self.home)
            if not steps:
                self.agent.ui.notice("nothing to submit against — the "
                                     "sidebar shows the next step")
                return
            self.push_screen(SubmitScreen(steps), self._do_submit)

        def _do_submit(self, args: dict | None) -> None:
            if not args:
                return
            ui = self.agent.ui

            def work() -> None:
                ui.tool_call("submit_work", args)
                ui.spinner("grading")
                try:
                    result = self.agent.tools.call("submit_work", args)
                    ui.stop_spinner()
                    ui.tool_result(result)
                except ToolError as e:
                    ui.stop_spinner()
                    ui.tool_result(str(e), error=True)
                self._from_any_thread(self.refresh_sidebar)

            self.run_worker(work, thread=True, exclusive=True)

    return SyApp
