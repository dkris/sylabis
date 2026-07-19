"""
Sandbox — containment for bundle-declared rubric scripts (Primetime
plan, Phase 1 WS3a). `sy attach <url>` + `sy submit` runs code the
bundle author wrote; before strangers publish bundles, that execution
must be contained.

Three layers, weakest never silently standing in for strongest:

1. **Environment scrub, always.** Scripts get an allowlisted env
   (PATH, HOME, TMPDIR) — never ANTHROPIC_API_KEY, never the rest of
   os.environ — regardless of backend.
2. **OS sandbox when available.** SandboxRunner wraps the subprocess
   in bwrap or nsjail when one is on PATH and actually works here
   (probed once): no network, read-only rootfs, tmpfs /tmp, and
   rlimits on CPU/memory/processes.
3. **Loud fallback.** No working sandbox (or `unsandboxed=True`, the
   documented escape hatch) runs the script directly with the scrubbed
   env and prints a warning naming the bundle's origin, so the learner
   knows exactly whose code just ran uncontained.

Consent lives here too: the first grade of an ATTACHED bundle that
declares rubric scripts must be explicitly approved once per bundle
(ConsentRequired / record_trust / trusted). The grader refuses to
execute scripts from an attached bundle without recorded trust.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .errors import SylabisError

# Environment allowlist — everything else (API keys, tokens, cloud
# credentials) is withheld from rubric scripts unconditionally.
ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR")

# Resource ceilings applied to the script process tree (best-effort
# rlimits set in the child after fork, so they bind the script but can
# never prevent the exec itself).
LIMITS = {
    "cpu_seconds": 300,          # matches grader.SCRIPT_TIMEOUT
    "memory_bytes": 4 << 30,     # 4 GiB address space
    "pids": 256,                 # forks available to the script
}

TRUST_MARKER = ".sylabis-trust"


class ConsentRequired(SylabisError):
    """An attached bundle declares rubric scripts and no trust has been
    recorded for it yet. Carries the script list and the bundle's origin
    so non-interactive surfaces (web/MCP/tools) can render the decision
    instead of hanging on a prompt. Nothing has been executed."""

    def __init__(self, source: str, scripts: list[str]):
        self.source = source
        self.scripts = list(scripts)
        super().__init__(
            f"the bundle attached from {source} declares rubric scripts "
            f"that grading would execute: {', '.join(self.scripts)}. "
            f"Approve once with `sylabis submit` (interactive) to record "
            f"trust for this bundle; nothing was executed.")


# ------------------------------------------------------------------ trust

def trusted(course_dir: Path) -> bool:
    """Has the learner consented to this bundle's rubric scripts?"""
    return (Path(course_dir) / TRUST_MARKER).exists()


def record_trust(course_dir: Path, source: str, scripts: list[str]) -> Path:
    """Record consent once per bundle: a .sylabis-trust marker inside the
    attached copy (dotfiles are structurally excluded from publish)."""
    marker = Path(course_dir) / TRUST_MARKER
    marker.write_text(yaml.dump({
        "schema": 1,
        "source": source,
        "scripts": list(scripts),
        "granted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, default_flow_style=False))
    return marker


# ---------------------------------------------------------------- runner

def scrubbed_env() -> dict:
    """The allowlisted environment rubric scripts run with. Never the
    API key, never the rest of os.environ."""
    return {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}


_probe_cache: dict[str, bool] = {}


def _probe_ok(tool: str) -> bool:
    """One cached no-op run per tool: a bwrap/nsjail binary on PATH is
    not proof it works here (user namespaces may be disabled). A broken
    sandbox must fall back loudly, not fail every grade mysteriously."""
    if tool not in _probe_cache:
        argv = {
            "bwrap": ["bwrap", "--unshare-all", "--ro-bind", "/", "/",
                      "--", "true"],
            "nsjail": ["nsjail", "-Mo", "--really_quiet", "--chroot", "/",
                       "--", "/bin/true"],
        }[tool]
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=15)
            _probe_cache[tool] = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _probe_cache[tool] = False
    return _probe_cache[tool]


def _apply_rlimits() -> None:  # pragma: no cover - runs in the child
    """Child-side resource ceilings (POSIX). Best-effort: a limit that
    cannot be set must not block grading — the timeout still binds."""
    try:
        import resource
    except ImportError:
        return
    for res_name, val in (("RLIMIT_CPU", LIMITS["cpu_seconds"]),
                          ("RLIMIT_AS", LIMITS["memory_bytes"]),
                          ("RLIMIT_NPROC", LIMITS["pids"])):
        res = getattr(resource, res_name, None)
        if res is None:
            continue
        try:
            _, hard = resource.getrlimit(res)
            cap = val if hard == resource.RLIM_INFINITY else min(val, hard)
            resource.setrlimit(res, (cap, cap))
        except (ValueError, OSError):
            pass


class SandboxRunner:
    """Pluggable containment for one grading run. The default backend is
    the strongest tool that is installed AND passes a probe: bwrap, then
    nsjail, then a direct subprocess with the scrubbed env and a loud
    warning naming the bundle's origin. `unsandboxed=True` (the
    documented escape hatch) forces the fallback and prints the same
    warning. Every backend gets the scrubbed env and rlimits."""

    def __init__(self, origin: str = "local bundle", unsandboxed: bool = False,
                 which=shutil.which, probe=_probe_ok):
        self.origin = origin
        self.unsandboxed = unsandboxed
        self.env = scrubbed_env()
        self.backend = "none"
        if not unsandboxed:
            for candidate in ("bwrap", "nsjail"):
                if which(candidate) and probe(candidate):
                    self.backend = candidate
                    break
        self._warned = False

    def command(self, argv: list[str], cwd: Path) -> list[str]:
        """The full command line for this backend — separated from run()
        so containment flags are testable without bwrap installed."""
        cwd = str(cwd)
        if self.backend == "bwrap":
            cmd = ["bwrap",
                   "--die-with-parent",
                   "--unshare-all",           # no network, new namespaces
                   "--ro-bind", "/", "/",     # read-only rootfs
                   "--dev", "/dev",
                   "--proc", "/proc",
                   "--tmpfs", "/tmp",         # scratch space, discarded
                   "--chdir", cwd,
                   "--clearenv"]
            for k, v in sorted(self.env.items()):
                cmd += ["--setenv", k, v]
            return cmd + ["--", *argv]
        if self.backend == "nsjail":
            cmd = ["nsjail", "-Mo", "--really_quiet",
                   "--chroot", "/",           # mounted read-only by default
                   "--cwd", cwd,
                   "--tmpfsmount", "/tmp",
                   "--rlimit_cpu", str(LIMITS["cpu_seconds"]),
                   "--rlimit_as", str(LIMITS["memory_bytes"] >> 20),  # MiB
                   "--rlimit_nproc", str(LIMITS["pids"])]
            for k, v in sorted(self.env.items()):
                cmd += ["--env", f"{k}={v}"]
            return cmd + ["--", *argv]
        return list(argv)

    def run(self, argv: list[str], cwd: Path,
            timeout: float | None = None) -> subprocess.CompletedProcess:
        """Run one script contained. May raise subprocess.TimeoutExpired
        (the caller flags it); everything else surfaces via returncode."""
        self._warn_if_uncontained()
        kwargs: dict = dict(cwd=cwd, env=self.env, capture_output=True,
                            text=True, timeout=timeout)
        if os.name == "posix":
            kwargs["preexec_fn"] = _apply_rlimits
        return subprocess.run(self.command(argv, cwd), **kwargs)

    def _warn_if_uncontained(self) -> None:
        if self.backend != "none" or self._warned:
            return
        self._warned = True
        why = ("--unsandboxed was requested" if self.unsandboxed
               else "no working sandbox found (install bwrap or nsjail)")
        print(f"WARNING: running rubric scripts from {self.origin} WITHOUT "
              f"an OS sandbox ({why}). The scripts get a scrubbed "
              f"environment (no API key) but full filesystem and network "
              f"access. Only proceed if you trust this bundle's author.",
              file=sys.stderr)
