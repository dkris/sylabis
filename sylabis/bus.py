"""
The agent event bus. The conversation loop (agent.py) emits typed events
as a turn unfolds — text deltas, tool calls, results — and any number of
subscribers observe them: the terminal renderer paints ANSI, a silent
harness subscribes nothing, and a future streaming web endpoint becomes
one more subscriber instead of a fork of the loop. The loop never knows
who is watching; that is the entire point.

Deliberately tiny: dataclass events and a synchronous callback list — the
loop is sequential, so pub/sub machinery beyond that would be decoration.
(Not to be confused with events.py, the on-disk domain event log.)
"""
from dataclasses import dataclass, field


@dataclass
class TurnStarted:
    """A user turn has begun."""


@dataclass
class ModelCallStarted:
    """One request to the model is in flight (a turn may hold several,
    one per tool round). Renderers show their 'thinking' state here."""


@dataclass
class TextDelta:
    """A fragment of streamed model prose."""
    text: str


@dataclass
class AssistantText:
    """A whole prose block at once (non-streaming model path)."""
    text: str


@dataclass
class ToolCallStarted:
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    name: str
    text: str
    error: bool = False


@dataclass
class TurnEnded:
    """The turn completed normally."""


@dataclass
class TurnInterrupted:
    """Ctrl-C: the turn was rolled back whole; nothing was kept."""


@dataclass
class TurnStopped:
    """The loop refused to continue (e.g. tool-round limit)."""
    reason: str


class Bus:
    def __init__(self):
        self._subscribers: list = []

    def subscribe(self, fn) -> None:
        """fn(event) is called synchronously, in subscription order."""
        self._subscribers.append(fn)

    def emit(self, event) -> None:
        for fn in self._subscribers:
            fn(event)
