"""
Terminal presentation for the agent harness. The organizing idea, borrowed
from modern agent CLIs: the transcript IS the interface. Model prose
streams in as it is generated; every tool call renders as one scannable
activity line with a short result preview underneath, so the learner sees
what the agent is doing without reading logs.

Pure ANSI — no curses, no dependencies. Degrades to plain text when
stdout is not a tty, NO_COLOR is set, or the console is disabled (the web
harness runs the same agent with a disabled console).
"""
import itertools
import json
import os
import shutil
import sys
import threading
import time

_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ARG_CLIP = 44      # per-value preview length inside a tool-call line
_RESULT_LINES = 4   # result preview height before "+N more"


def _term_width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def preview_args(args: dict) -> str:
    """One-line, human-scannable argument list. Long strings (artifacts,
    reflections) collapse to a length note — the learner wrote them, they
    don't need them echoed."""
    parts = []
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > _ARG_CLIP:
            parts.append(f'{k}: "{v[:24].rstrip()}…" ({len(v)} chars)')
        else:
            parts.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    return ", ".join(parts)


def preview_result(text: str, limit: int = _RESULT_LINES) -> list[str]:
    """The first few meaningful lines of a tool result, plus a count of
    what was elided."""
    lines = [l.rstrip() for l in (text or "").strip().splitlines() if l.strip()]
    if not lines:
        return ["(empty)"]
    width = max(_term_width() - 8, 40)
    shown = [l if len(l) <= width else l[:width - 1] + "…"
             for l in lines[:limit]]
    if len(lines) > limit:
        shown.append(f"… +{len(lines) - limit} more lines")
    return shown


class Console:
    """All output goes through here. `enabled=False` turns every method
    into a no-op so other harnesses can drive the same agent silently."""

    def __init__(self, enabled: bool = True, file=None):
        self.file = file or sys.stdout
        self.enabled = enabled
        self.color = (enabled and hasattr(self.file, "isatty")
                      and self.file.isatty()
                      and not os.environ.get("NO_COLOR")
                      and os.environ.get("TERM") != "dumb")
        self._spinner = None

    # ------------------------------------------------------------- painting

    def _c(self, code: str, s: str) -> str:
        return f"\x1b[{code}m{s}\x1b[0m" if self.color else s

    def dim(self, s):    return self._c("2", s)
    def bold(self, s):   return self._c("1", s)
    def cyan(self, s):   return self._c("36", s)
    def red(self, s):    return self._c("31", s)
    def green(self, s):  return self._c("32", s)

    def _out(self, s: str = "", end: str = "\n") -> None:
        if not self.enabled:
            return
        self.stop_spinner()
        print(s, end=end, file=self.file, flush=True)

    # ------------------------------------------------------------ the trace

    def header(self, title: str, lines: list[str]) -> None:
        self._out(f"\n{self.cyan('◆')} {self.bold(title)}")
        for line in lines:
            self._out(f"  {self.dim(line)}")
        self._out()

    def text_delta(self, s: str) -> None:
        """Streamed model prose, printed as it generates."""
        if not self.enabled:
            return
        self.stop_spinner()
        print(s, end="", file=self.file, flush=True)

    def text_block(self, s: str) -> None:
        """A whole reply at once (non-streaming model)."""
        self._out(s)

    def turn_start(self) -> None:
        self._out()

    def turn_end(self) -> None:
        self._out()

    def tool_call(self, name: str, args: dict) -> None:
        line = f"{self.cyan('⏺')} {self.bold(name)}"
        detail = preview_args(args)
        if detail:
            line += self.dim(f"({detail})")
        else:
            line += self.dim("()")
        self._out("\n" + line)

    def tool_result(self, text: str, error: bool = False) -> None:
        lines = preview_result(text)
        paint = self.red if error else self.dim
        self._out(f"  {paint('⎿')}  {paint(lines[0])}")
        for l in lines[1:]:
            self._out(f"      {paint(l)}")

    def notice(self, s: str) -> None:
        self._out(self.dim(f"  {s}"))

    def error(self, s: str) -> None:
        self._out(self.red(f"  {s}"))

    # -------------------------------------------------------------- spinner

    def spinner(self, label: str) -> "_Spinner":
        """Start an activity spinner; it stops itself at the next output
        (first streamed token, tool line) or explicitly via stop()."""
        self.stop_spinner()
        self._spinner = _Spinner(self, label)
        self._spinner.start()
        return self._spinner

    def stop_spinner(self) -> None:
        if self._spinner is not None:
            sp, self._spinner = self._spinner, None
            sp.stop()


class ConsoleRenderer:
    """The Console as a bus subscriber: translates agent turn events into
    the trace. This is the only place event types meet ANSI painting — a
    different front end subscribes its own renderer and console.py never
    hears about it."""

    def __init__(self, console: Console):
        self.console = console

    def __call__(self, event) -> None:
        from . import bus  # local: console must stay importable alone
        ui = self.console
        if isinstance(event, bus.TurnStarted):
            ui.turn_start()
        elif isinstance(event, bus.ModelCallStarted):
            ui.spinner("thinking")
        elif isinstance(event, bus.TextDelta):
            ui.text_delta(event.text)
        elif isinstance(event, bus.AssistantText):
            ui.text_block(event.text)
        elif isinstance(event, bus.ToolCallStarted):
            ui.tool_call(event.name, event.args)
            ui.spinner(event.name)
        elif isinstance(event, bus.ToolResult):
            ui.stop_spinner()
            ui.tool_result(event.text, error=event.error)
        elif isinstance(event, bus.TurnEnded):
            ui.stop_spinner()
        elif isinstance(event, bus.TurnInterrupted):
            ui.stop_spinner()
            ui.notice("interrupted — this turn was rolled back")
        elif isinstance(event, bus.TurnStopped):
            ui.stop_spinner()
            ui.error(event.reason)


class _Spinner:
    def __init__(self, console: Console, label: str):
        self.console = console
        self.label = label
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if not (self.console.enabled and self.console.color):
            return  # never animate into a pipe or a dumb terminal
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self) -> None:
        for frame in itertools.cycle(_FRAMES):
            if self._stop.wait(0.08):
                return
            print(f"\r\x1b[2m{frame} {self.label}…\x1b[0m\x1b[K",
                  end="", file=self.console.file, flush=True)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        print("\r\x1b[K", end="", file=self.console.file, flush=True)
