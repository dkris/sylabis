"""
WS2 packaging/distribution suite: single-sourced version (`sy --version`,
MCP serverInfo), the `serve --mock` offline MCP path, narrowed protocol
version negotiation, the update-check cache/guard logic (injected fake
fetcher — no network, ever), the SylabisError top-level CLI handler, and
the install.sh release-tag pin.

Fully offline and deterministic, same conventions as run_all.py:
top-level test_* functions, optional (tmp: Path) arg.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from sylabis import __version__
from sylabis import cli
from sylabis import update_check
from sylabis.errors import SylabisError
from sylabis.mcp_server import LATEST_PROTOCOL, PROTOCOL_VERSIONS, MCPServer

ROOT = Path(__file__).parent.parent

# The update check must stay inert no matter where its guards are probed
# from; belt-and-braces for every subprocess this suite spawns.
_QUIET_ENV = {**os.environ, "SYLABIS_NO_UPDATE_CHECK": "1"}


def _home(tmp: Path) -> Path:
    home = tmp / "home"
    (home / "courses").mkdir(parents=True)
    return home


# ------------------------------------------------------------- versioning

def test_cli_version_is_single_sourced():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            cli.main(["--version"])
        except SystemExit as e:
            assert not e.code, "--version must exit 0"
    assert __version__ in buf.getvalue(), "sy --version prints __version__"


def test_pyproject_reads_version_dynamically():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in text
    assert "sylabis.__version__" in text, "version single-sourced from the package"
    assert re.search(r'^version\s*=\s*"', text, re.M) is None, \
        "no hardcoded [project] version may survive"


def test_mcp_serverinfo_reports_package_version(tmp):
    srv = MCPServer(None, mock=True, home_dir=_home(tmp))
    result = srv._dispatch("initialize", {"protocolVersion": LATEST_PROTOCOL})
    assert result["serverInfo"]["version"] == __version__, \
        "serverInfo must not drift from the package version"


# ---------------------------------------------------- MCP over the real CLI

def test_cli_serve_mock_answers_initialize_offline(tmp):
    """`python -m sylabis.cli serve --mock` is a working MCP stdio server
    with no API key and no network — the full entry-point path."""
    home = _home(tmp)
    req = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}})
    env = dict(_QUIET_ENV)
    env.pop("ANTHROPIC_API_KEY", None)  # offline means offline
    proc = subprocess.run(
        [sys.executable, "-m", "sylabis.cli", "serve", "--mock",
         "--home", str(home)],
        input=req + "\n", capture_output=True, text=True,
        cwd=ROOT, env=env, timeout=120)
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"stdout must carry protocol only: {proc.stdout!r}"
    resp = json.loads(lines[0])
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert resp["result"]["serverInfo"] == {"name": "sylabis",
                                            "version": __version__}


def test_mcp_version_negotiation_narrowed(tmp):
    srv = MCPServer(None, mock=True, home_dir=_home(tmp))

    # Every supported version is echoed back per-spec.
    for v in PROTOCOL_VERSIONS:
        r = srv._dispatch("initialize", {"protocolVersion": v})
        assert r["protocolVersion"] == v

    # A well-formed future version gets LATEST (per-spec: the client
    # decides whether to proceed) but is flagged loudly, not silently.
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        r = srv._dispatch("initialize", {"protocolVersion": "2099-01-01"})
    assert r["protocolVersion"] == LATEST_PROTOCOL
    assert "2099-01-01" in err.getvalue() and "unsupported" in err.getvalue(), \
        "an unknown version must be flagged, never silently answered"

    # Malformed or missing versions are rejected as invalid params.
    for params in ({}, {"protocolVersion": None},
                   {"protocolVersion": 42}, {"protocolVersion": "banana"}):
        resp = srv._handle_line(json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "initialize",
             "params": params}))
        assert "error" in resp and resp["error"]["code"] == -32602, \
            f"must reject protocolVersion in {params!r}"


# ------------------------------------------------------------ update check

def test_update_check_notice_and_daily_cache(tmp):
    calls = []

    def fetch():
        calls.append(1)
        return "9.9.9"

    notice = update_check.check(tmp, fetch=fetch, env={}, now=1000.0,
                                current="0.2.0")
    assert notice and "9.9.9" in notice and "0.2.0" in notice
    assert "upgrade" in notice.lower(), "notice tells, never self-updates"
    assert (tmp / update_check.CACHE_NAME).exists(), "cache lands in home"

    # Within the TTL the cache answers; the fetcher is not called again.
    notice = update_check.check(tmp, fetch=fetch, env={},
                                now=1000.0 + 3600, current="0.2.0")
    assert notice and len(calls) == 1, "one fetch per day, max"

    # Past the TTL it refetches once.
    update_check.check(tmp, fetch=fetch, env={},
                       now=1000.0 + update_check.CACHE_TTL + 1,
                       current="0.2.0")
    assert len(calls) == 2


def test_update_check_guards_and_silence(tmp):
    def must_not_fetch():
        raise AssertionError("guarded check must never hit the network")

    # Env kill-switches: ours, the standard one, and CI.
    for guard in ("SYLABIS_NO_UPDATE_CHECK", "DO_NOT_TRACK",
                  "CI", "GITHUB_ACTIONS"):
        assert update_check.check(tmp / "a", fetch=must_not_fetch,
                                  env={guard: "1"}, now=0.0) is None
    assert not (tmp / "a").exists(), "guarded runs write nothing"

    # Up to date: no notice.
    assert update_check.check(tmp / "b", fetch=lambda: "0.2.0", env={},
                              now=0.0, current="0.2.0") is None
    # A dev build of the next release is NOT behind the current release.
    assert update_check.check(tmp / "c", fetch=lambda: "0.2.0", env={},
                              now=0.0, current="0.3.0.dev0") is None
    # ...but a dev build is behind its own final release.
    assert update_check.check(tmp / "d", fetch=lambda: "0.2.0", env={},
                              now=0.0, current="0.2.0.dev0")

    # Fetch failure (offline, PyPI down): silence, never an error.
    def boom():
        raise OSError("no network")
    assert update_check.check(tmp / "e", fetch=boom, env={}, now=0.0) is None

    # A corrupt cache file is treated as a miss, not a crash.
    (tmp / "f").mkdir()
    (tmp / "f" / update_check.CACHE_NAME).write_text("not json{")
    assert update_check.check(tmp / "f", fetch=lambda: "9.9.9", env={},
                              now=0.0, current="0.2.0")


def test_update_check_never_speaks_to_pipes(tmp):
    """maybe_notify is the CLI hook: with stderr a pipe (as in every test
    and CI run) it must return without fetching or writing anything."""
    def must_not_fetch():
        raise AssertionError("non-tty run must never check")

    real = update_check._fetch_latest
    update_check._fetch_latest = must_not_fetch
    try:
        err = io.StringIO()  # StringIO.isatty() is False, like a pipe
        with contextlib.redirect_stderr(err):
            update_check.maybe_notify(tmp)
        assert err.getvalue() == ""
        assert not (tmp / update_check.CACHE_NAME).exists()
    finally:
        update_check._fetch_latest = real


# --------------------------------------------------------- CLI error policy

def test_cli_turns_sylabis_error_into_one_line_exit_1():
    def explode(argv=None):
        raise SylabisError("intake declined: topic not viable")

    real = cli._main
    cli._main = explode
    try:
        try:
            cli.main([])
        except SystemExit as e:
            # sys.exit(str) semantics: message printed to stderr, status 1.
            assert isinstance(e.code, str)
            assert "intake declined" in e.code and "\n" not in e.code
        else:
            raise AssertionError("SylabisError must become SystemExit(1)")
    finally:
        cli._main = real


def test_cli_serve_never_swallows_home_or_mock(tmp):
    """The serve wiring regression WS2 fixed: --mock must reach MCPServer
    (mock course tools, no Anthropic client construction)."""
    home = _home(tmp)
    captured = {}

    import sylabis.mcp_server as mcp_mod
    real_run = mcp_mod.MCPServer.run

    def spy_run(self):
        captured["mock"] = self.mock
        captured["course_dir"] = self.course_dir

    mcp_mod.MCPServer.run = spy_run
    try:
        cli.main(["serve", "--mock", "--home", str(home)])
    finally:
        mcp_mod.MCPServer.run = real_run
    assert captured == {"mock": True, "course_dir": None}


# ------------------------------------------------------------ distribution

def test_install_sh_is_pinned_to_a_release_tag():
    text = (ROOT / "install.sh").read_text()
    m = re.search(r'REF="\$\{SYLABIS_REF:-([^}"]+)\}"', text)
    assert m, "install.sh must default REF via SYLABIS_REF"
    ref = m.group(1)
    assert ref != "main", "curl|sh from a moving branch is unauditable"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", ref), \
        f"REF default must be a release tag, got {ref!r}"


def test_entry_points_declared():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'sylabis-mcp = "sylabis.cli:mcp_main"' in text
    assert 'sy = "sylabis.cli:main"' in text
    assert hasattr(cli, "mcp_main"), "console script target must exist"
    assert '"AGPL-3.0-only"' in text, "license metadata is part of the release"
    assert 'tui = ["textual>=1.0"]' in text
