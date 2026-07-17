"""
Configuration — where the API key lives and which model to talk to.
Setup used to be the sharpest edge of the whole tool: the key had to be
exported by hand and `.env` only loaded from the current directory. Now
`sy init` saves the key once into $SYLABIS_HOME/.env and every command
finds it from anywhere.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-4-6"

# The canonical sylabis repository — the single source for everything that
# points a user (or a CI workflow) back at the project. install.sh must
# agree; tests pin the two together.
SYLABIS_REPO = "https://github.com/dkris/sylabis"


def home() -> Path:
    """The journey home, config edition: $SYLABIS_HOME or ~/sylabis.
    (journey.home also honors a --home flag; config state always lives in
    the environment-selected home so every command finds the same key.)"""
    return Path(os.environ.get("SYLABIS_HOME", str(Path.home() / "sylabis")))


def load_env() -> None:
    """Load .env from the current directory first (dev checkout compat),
    then from $SYLABIS_HOME/.env. dotenv never overrides what is already
    set, so precedence is: real environment > cwd .env > home .env.
    A cwd .env that sets SYLABIS_HOME is honored because cwd loads first."""
    load_dotenv()
    load_dotenv(home() / ".env")


def save_key(key: str, home_dir: Path | str | None = None) -> Path:
    """Merge ANTHROPIC_API_KEY=... into $SYLABIS_HOME/.env, preserving any
    other lines. The file is chmod 0600 — it holds a credential."""
    env_path = Path(home_dir) / ".env" if home_dir else home() / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if env_path.exists():
        lines = [l for l in env_path.read_text().splitlines()
                 if not l.startswith("ANTHROPIC_API_KEY=")]
    lines.append(f"ANTHROPIC_API_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n")
    os.chmod(env_path, 0o600)
    return env_path


def model() -> str:
    """The model to use — $SYLABIS_MODEL overrides so a deprecated default
    id never bricks an install. Read at call time, not import time."""
    return os.environ.get("SYLABIS_MODEL", DEFAULT_MODEL)
