"""
Update etiquette (Primetime plan, Phase 1 WS2.5). A once-daily, cached
check against PyPI that prints a one-line notice when a newer sylabis
exists. That is ALL it does:

- **No telemetry.** The request is a plain GET of the public PyPI JSON
  endpoint — no identifiers, no payload, nothing about the learner or
  the journey ever leaves the machine.
- **No silent self-update.** The notice tells you the upgrade command;
  running it is your call.
- Disabled entirely by `SYLABIS_NO_UPDATE_CHECK` or `DO_NOT_TRACK`
  (any non-empty value), and never runs in CI (`CI`/`GITHUB_ACTIONS`).
- The CLI additionally only surfaces the notice on an interactive
  terminal and never in `--mock` runs or the MCP `serve` transport
  (stdout there carries protocol messages only) — see `cli.main`.
- The cache lives at `$SYLABIS_HOME/.update-check.json` so at most one
  request per day is ever made.

Everything is injectable (`fetch`, `env`, `now`) so the test suite
exercises the cache and guard logic fully offline.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__

PYPI_URL = "https://pypi.org/pypi/sylabis/json"
CACHE_NAME = ".update-check.json"
CACHE_TTL = 24 * 60 * 60  # one day, in seconds
FETCH_TIMEOUT = 3.0

_GUARDS = ("SYLABIS_NO_UPDATE_CHECK", "DO_NOT_TRACK", "CI", "GITHUB_ACTIONS")


def _disabled(env) -> bool:
    return any(env.get(name) for name in _GUARDS)


def _parse_version(v: str) -> tuple:
    """(numeric segments, is_final_release). Enough ordering for a
    notice: 0.2.0 > 0.2.0.dev1, 0.10.0 > 0.9.9. Not a full PEP 440
    comparator — a wrong answer here costs one line of stderr."""
    m = re.match(r"(\d+(?:\.\d+)*)", v or "")
    if not m:
        return ((), 0)
    nums = tuple(int(x) for x in m.group(1).split("."))
    return (nums, 1 if m.group(1) == v.strip() else 0)


def _newer(latest: str, current: str) -> bool:
    (ln, lf), (cn, cf) = _parse_version(latest), _parse_version(current)
    width = max(len(ln), len(cn))
    ln += (0,) * (width - len(ln))
    cn += (0,) * (width - len(cn))
    return (ln, lf) > (cn, cf)


def _fetch_latest() -> str | None:
    import urllib.request  # stdlib; no new dependency for a courtesy notice
    with urllib.request.urlopen(PYPI_URL, timeout=FETCH_TIMEOUT) as resp:
        return json.load(resp).get("info", {}).get("version")


def check(home: Path | str, *, fetch=None, env=None, now: float | None = None,
          current: str = __version__) -> str | None:
    """Return the one-line notice, or None. Never raises, never blocks
    longer than the fetch timeout, and hits the network at most once
    per CACHE_TTL thanks to the on-disk cache under `home`."""
    env = os.environ if env is None else env
    if _disabled(env):
        return None
    now = time.time() if now is None else now
    cache = Path(home) / CACHE_NAME

    latest = None
    try:
        data = json.loads(cache.read_text())
        if 0 <= now - float(data.get("ts", 0)) < CACHE_TTL:
            latest = data.get("latest")
    except (OSError, ValueError):
        pass

    if not latest:
        try:
            latest = (fetch or _fetch_latest)()
        except Exception:
            return None  # offline or PyPI down: silence, never an error
        if not latest:
            return None
        try:
            Path(home).mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"ts": now, "latest": latest}))
        except OSError:
            pass  # unwritable home just means we re-check next run

    if _newer(latest, current):
        return (f"sylabis {latest} is out (you have {current}). Upgrade: "
                f"uv tool upgrade sylabis — or pipx upgrade sylabis. "
                f"Set SYLABIS_NO_UPDATE_CHECK=1 to silence this.")
    return None


def maybe_notify(home: Path | str) -> None:
    """CLI hook: print the notice to stderr, and only when stderr is an
    interactive terminal — piped runs, scripts, and the test suite never
    see (or trigger) the check. Never raises."""
    try:
        if not sys.stderr.isatty():
            return
        notice = check(home)
        if notice:
            print(notice, file=sys.stderr)
    except Exception:
        pass  # a courtesy notice must never break a real command
