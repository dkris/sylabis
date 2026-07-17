"""
Git layer — course bundles as repositories. Compiled bundles get a repo
from birth, submits commit the work, `publish` puts the bundle on GitHub
(where grade.yml turns every push of work into graded feedback), and
`sync` pulls CI-graded state back down.

Git here is an enhancement, never a requirement: every function no-ops or
soft-fails when git is absent, and a git problem must never block or alter
a grade. Failures raise GitError with a message a learner can act on —
never a traceback. No LLM calls, no new dependencies: subprocess git plus
the `gh` CLI when it happens to be installed.
"""
import re
import shutil
import subprocess
from pathlib import Path

from . import events


class GitError(Exception):
    """Git ran but could not complete. The message is the remedy."""


def _run(cdir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(cdir), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cdir}: "
                       f"{(proc.stderr or proc.stdout).strip()}")
    return proc


def has_git() -> bool:
    return shutil.which("git") is not None


def has_gh() -> bool:
    return shutil.which("gh") is not None


def is_repo(cdir: Path) -> bool:
    return (Path(cdir) / ".git").exists()


GITIGNORE = ".env\n__pycache__/\n*.pyc\n.DS_Store\n"
# events.jsonl is append-only, so union-merging both sides is always
# correct — this removes the likeliest sync conflict outright.
GITATTRIBUTES = "events.jsonl merge=union\n"


def ensure_repo(cdir: Path) -> bool:
    """Make the bundle a git repository (no commit — the caller commits so
    late-written files like events.jsonl land in the initial snapshot).
    Returns True only when a repo was actually created. No-ops when git is
    missing, the bundle is already a repo, the bundle is a symlink into
    someone else's directory (attach), or the directory already sits inside
    another repo (never nest)."""
    cdir = Path(cdir)
    if not has_git() or cdir.is_symlink() or is_repo(cdir):
        return False
    inside = _run(cdir, "rev-parse", "--show-toplevel", check=False)
    if inside.returncode == 0:
        return False  # already inside some other repo — don't nest
    (cdir / ".gitignore").write_text(GITIGNORE)
    (cdir / ".gitattributes").write_text(GITATTRIBUTES)
    init = _run(cdir, "init", "-q", "-b", "main", check=False)
    if init.returncode != 0:  # git < 2.28 has no -b
        _run(cdir, "init", "-q")
    return True


def _identity_args(cdir: Path) -> list[str]:
    """A commit identity fallback used only when the learner has none
    configured — never clobbers a real identity."""
    if _run(cdir, "config", "user.email", check=False).returncode == 0:
        return []
    return ["-c", "user.name=sylabis", "-c", "user.email=sylabis@localhost"]


def commit_all(cdir: Path, message: str) -> bool:
    """Commit everything, or nothing: skip-when-clean is the idempotency
    contract — re-running never stacks empty commits. `.env` files are
    excluded by pathspec, not just .gitignore, so a learner's key can
    never be committed by sylabis even in a repo missing our ignore file
    (e.g. an attached clone). Returns True only when a commit was made."""
    cdir = Path(cdir)
    if not has_git() or not is_repo(cdir):
        return False
    if not _run(cdir, "status", "--porcelain").stdout.strip():
        return False
    _run(cdir, "add", "-A", "--", ".",
         ":(exclude).env", ":(exclude)**/.env")
    proc = subprocess.run(["git", "-C", str(cdir), *_identity_args(cdir),
                           "commit", "-q", "-m", message],
                          capture_output=True, text=True, check=False)
    return proc.returncode == 0


def remote_url(cdir: Path) -> str | None:
    # The raw configured URL — `remote get-url` applies url.insteadOf
    # rewrites (proxies, mirrors), which must never leak into share links.
    proc = _run(cdir, "config", "--get", "remote.origin.url", check=False)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def set_remote(cdir: Path, url: str) -> None:
    if remote_url(cdir):
        _run(cdir, "remote", "set-url", "origin", url)
    else:
        _run(cdir, "remote", "add", "origin", url)


def current_branch(cdir: Path) -> str:
    return _run(cdir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def head_sha(cdir: Path) -> str | None:
    proc = _run(cdir, "rev-parse", "HEAD", check=False)
    return proc.stdout.strip() if proc.returncode == 0 else None


def push(cdir: Path) -> None:
    proc = _run(cdir, "push", "-q", "-u", "origin", "HEAD", check=False)
    if proc.returncode != 0:
        raise GitError(
            f"Could not push {Path(cdir).name}: "
            f"{(proc.stderr or proc.stdout).strip()}\n"
            f"Fix the remote or your access, then re-run `sy publish`.")


def pull_ff(cdir: Path) -> bool:
    """Fast-forward-only pull; True when it moved HEAD. Divergence gets a
    friendly remedy, never an automatic merge or anything destructive."""
    before = head_sha(cdir)
    proc = _run(cdir, "pull", "--ff-only", "-q", "origin",
                current_branch(cdir), check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout).strip()
        if "fast-forward" in err or "divergent" in err.lower():
            raise GitError(
                f"{Path(cdir).name} has diverged from its remote.\n"
                f"Resolve it manually: git -C {cdir} pull --rebase")
        raise GitError(f"Could not pull {Path(cdir).name}: {err}")
    return head_sha(cdir) != before


_WEB_URL = re.compile(
    r"^(?:https://(?P<host>[^/]+)/|ssh://(?:[^@/]+@)?(?P<sshurlhost>[^/:]+)"
    r"(?::\d+)?/|git@(?P<sshhost>[^:]+):)"
    r"(?P<path>.+?)(?:\.git)?/?$")


def web_url(remote: str | None) -> str | None:
    """Browser URL for a remote — pure string transform, total function:
    anything unrecognized (file://, local paths) returns None. Userinfo
    (tokens embedded in https remotes) never reaches a share link."""
    if not remote or remote.startswith("file://"):
        return None
    m = _WEB_URL.match(remote)
    if not m:
        return None
    host = m.group("host") or m.group("sshurlhost") or m.group("sshhost")
    host = host.rsplit("@", 1)[-1]  # strip user:token@ credentials
    return f"https://{host}/{m.group('path')}"


def publish(cdir: Path, repo_url: str | None = None,
            private: bool = True) -> dict:
    """Put the bundle on a remote: commit what's here, then push to origin —
    wiring origin first from --repo, or creating a GitHub repo via the gh
    CLI when it's installed. Without either, the GitError IS the recipe.
    Returns {remote, remote_set, pushed_new}."""
    cdir = Path(cdir)
    if not has_git():
        raise GitError("git is not installed — install git, then re-run "
                       "`sy publish`.")
    ensure_repo(cdir)
    if not is_repo(cdir):
        raise GitError(f"{cdir.name} could not become a git repository "
                       "(is it a symlinked course? publish it from its "
                       "own directory).")
    commit_all(cdir, "publish: snapshot")

    remote_set = False
    if not remote_url(cdir):
        if repo_url:
            if not repo_url.startswith(("https://", "git@", "ssh://", "file://")):
                raise GitError(f"{repo_url!r} does not look like a git URL.")
            set_remote(cdir, repo_url)
            remote_set = True
        elif has_gh():
            vis = "--private" if private else "--public"
            proc = subprocess.run(
                ["gh", "repo", "create", cdir.name, vis,
                 "--source", str(cdir), "--push"],
                capture_output=True, text=True)
            # Trust the outcome, not gh's stdout: did origin appear?
            if proc.returncode != 0 or not remote_url(cdir):
                raise GitError(_MANUAL_RECIPE.format(
                    name=cdir.name, cdir=cdir,
                    why=f"gh repo create failed: "
                        f"{(proc.stderr or proc.stdout).strip()}"))
            remote_set = True
        else:
            raise GitError(_MANUAL_RECIPE.format(
                name=cdir.name, cdir=cdir,
                why="No remote is set and the gh CLI is not installed."))

    before = _run(cdir, "rev-parse", f"origin/{current_branch(cdir)}",
                  check=False)
    pushed_new = (remote_set or before.returncode != 0
                  or before.stdout.strip() != head_sha(cdir))
    if pushed_new:
        # The event rides inside the pushed history — emitted after it,
        # it would dirty the tree and re-publish would never converge.
        events.emit(cdir, "course.published", {"remote": remote_url(cdir)})
        commit_all(cdir, "publish: record")
    push(cdir)
    return {"remote": remote_url(cdir), "remote_set": remote_set,
            "pushed_new": pushed_new}


_MANUAL_RECIPE = """{why}
Publish {name!r} in three steps:
  1. Create an empty repository (e.g. https://github.com/new — private is fine)
  2. git -C {cdir} remote add origin <its URL>
  3. Re-run: sy publish"""


def sync(cdir: Path) -> dict:
    """Commit local work, pull CI-graded state (ff-only), push. Returns
    {committed, pulled, pushed}. Requires a published course. The
    course.synced event (only when something moved) is committed as part
    of the push, so a quiet sync stays a no-op forever."""
    cdir = Path(cdir)
    if not (has_git() and is_repo(cdir) and remote_url(cdir)):
        raise GitError(f"{cdir.name} is not published — run `sy publish` "
                       "first.")
    committed = commit_all(cdir, "sync: local work")
    pulled = pull_ff(cdir)
    before = _run(cdir, "rev-parse", f"origin/{current_branch(cdir)}",
                  check=False).stdout.strip()
    pushing = committed or before != head_sha(cdir)
    if pulled or pushing:
        events.emit(cdir, "course.synced",
                    {"remote": remote_url(cdir),
                     "pulled": pulled, "pushed": pushing})
        commit_all(cdir, "sync: record")
    push(cdir)
    return {"committed": committed, "pulled": pulled, "pushed": pushing}
