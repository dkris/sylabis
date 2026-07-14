"""
Trust — the execution boundary. Rubric scripts are real code that runs on
the learner's machine, and `attach` can bring a course in from any git
URL. So execution consent is explicit and per-course: compiling a course
into your own journey grants it (you asked for that code to exist);
anything attached from elsewhere stays untrusted until the learner says
otherwise (`sylabis trust <course>` or `attach --trust`).

The registry lives at <journey home>/trust.yaml, keyed by the course's
resolved real path — a marker inside the bundle would be forgeable by the
very repo we're refusing to trust. Explicit single-course invocations
(`sylabis grade DIR MID`, `serve DIR`) are not gated: naming the
directory yourself is the consent, and the CI grading workflow runs in
the learner's own repo.
"""
import time
from pathlib import Path

import yaml

FILE = "trust.yaml"


def _registry(home_dir: Path) -> Path:
    return Path(home_dir) / FILE


def _load(home_dir: Path) -> dict:
    path = _registry(home_dir)
    if not path.exists():
        return {"trusted": []}
    data = yaml.safe_load(path.read_text()) or {}
    data.setdefault("trusted", [])
    return data


def _key(course_dir: Path) -> str:
    # Resolve so a symlinked attach and its target are the same decision.
    return str(Path(course_dir).resolve())


def grant(home_dir: Path, course_dir: Path, origin: str) -> None:
    """Record consent for one course's executable grading. Idempotent."""
    home_dir = Path(home_dir)
    data = _load(home_dir)
    key = _key(course_dir)
    if any(e.get("path") == key for e in data["trusted"]):
        return
    data["trusted"].append({
        "path": key,
        "origin": origin,
        "granted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    home_dir.mkdir(parents=True, exist_ok=True)
    _registry(home_dir).write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False))


def is_trusted(home_dir: Path, course_dir: Path) -> bool:
    key = _key(course_dir)
    return any(e.get("path") == key for e in _load(home_dir)["trusted"])


def entries(home_dir: Path) -> list[dict]:
    return _load(home_dir)["trusted"]
