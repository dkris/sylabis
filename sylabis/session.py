"""
Session persistence — the conversation survives the process. One flat
JSONL file at <journey home>/session.jsonl, one message per line, loaded
on start and appended after each completed turn. The journey itself (the
durable record) lives in the course bundles; this file holds only the
chat, so losing it costs a conversation, never progress.

Deliberately flat: one active conversation per journey, no branching or
forking — resuming a talk with your tutor is not the same problem as
forking a coding agent's decision tree. /clear truncates it.
"""
import json
from pathlib import Path

FILE = "session.jsonl"


def _clean_content(content):
    """Keep only the fields the Messages API needs; SDK model_dump()
    output carries nulls and extras that don't belong in a durable file."""
    if isinstance(content, str):
        return content
    cleaned = []
    for block in content:
        t = block.get("type")
        if t == "text":
            cleaned.append({"type": "text", "text": block.get("text", "")})
        elif t == "tool_use":
            cleaned.append({"type": "tool_use", "id": block["id"],
                            "name": block["name"],
                            "input": block.get("input", {})})
        elif t == "tool_result":
            cleaned.append({"type": "tool_result",
                            "tool_use_id": block["tool_use_id"],
                            "content": block.get("content", ""),
                            "is_error": bool(block.get("is_error"))})
        else:
            cleaned.append(block)
    return cleaned


class Session:
    def __init__(self, home_dir: Path):
        self.path = Path(home_dir) / FILE

    def load(self) -> list[dict]:
        """The saved transcript, oldest first. A corrupt file is set aside
        (.bad) and the conversation starts fresh — never a crash."""
        if not self.path.exists():
            return []
        messages = []
        try:
            for line in self.path.read_text().splitlines():
                if line.strip():
                    messages.append(json.loads(line))
        except json.JSONDecodeError:
            self.path.rename(self.path.with_suffix(".jsonl.bad"))
            return []
        return messages

    def append(self, messages: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a") as f:
            for m in messages:
                f.write(json.dumps(
                    {"role": m["role"],
                     "content": _clean_content(m["content"])}) + "\n")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
