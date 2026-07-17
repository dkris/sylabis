#!/bin/sh
# sylabis installer — sets up the `sy` learning agent.
#
#   curl -fsSL https://raw.githubusercontent.com/dkris/sylabis/main/install.sh | sh
#
# What it does:
#   1. Finds a Python 3.10+ interpreter.
#   2. Creates an isolated virtualenv under ~/.local/share/sylabis/venv
#      (respects $XDG_DATA_HOME; override the whole dir with $SYLABIS_INSTALL_DIR).
#   3. Installs sylabis into it from GitHub.
#   4. Links `sy` and `sylabis` into ~/.local/bin (override with $SYLABIS_BIN_DIR)
#      and makes sure that directory is on your PATH.
#
# Re-run the same command any time to upgrade. Uninstall with:
#   curl -fsSL https://raw.githubusercontent.com/dkris/sylabis/main/install.sh | sh -s -- --uninstall

set -eu

REPO_URL="${SYLABIS_REPO:-https://github.com/dkris/sylabis}"
REF="${SYLABIS_REF:-main}"
# Anything pip understands: a URL, a local path, a VCS spec. Defaults to the
# GitHub tarball of $REF so the installer needs curl-ish networking but not git.
SOURCE="${SYLABIS_SOURCE:-${REPO_URL}/archive/${REF}.tar.gz}"

DATA_DIR="${SYLABIS_INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/sylabis}"
VENV_DIR="$DATA_DIR/venv"
BIN_DIR="${SYLABIS_BIN_DIR:-$HOME/.local/bin}"

# ---------- output helpers ----------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD="$(printf '\033[1m')"; DIM="$(printf '\033[2m')"
    GREEN="$(printf '\033[32m')"; RED="$(printf '\033[31m')"
    RESET="$(printf '\033[0m')"
else
    BOLD=""; DIM=""; GREEN=""; RED=""; RESET=""
fi

say()  { printf '%s\n' "${1}"; }
step() { printf '%s\n' "${DIM}·${RESET} ${1}"; }
ok()   { printf '%s\n' "${GREEN}✓${RESET} ${1}"; }
fail() { printf '%s\n' "${RED}✗ ${1}${RESET}" >&2; exit 1; }

# ---------- uninstall ----------------------------------------------------

uninstall() {
    removed=0
    for name in sy sylabis; do
        link="$BIN_DIR/$name"
        # Only remove links we own: ones that resolve into our venv.
        if [ -L "$link" ] && ls -l "$link" 2>/dev/null | grep -q "$VENV_DIR/bin/"; then
            rm -f "$link"
            removed=1
        fi
    done
    if [ -d "$DATA_DIR" ]; then
        rm -rf "$DATA_DIR"
        removed=1
    fi
    if [ "$removed" = 1 ]; then
        ok "sylabis uninstalled ($DATA_DIR removed)."
        say "  ${DIM}Your journey in \${SYLABIS_HOME:-~/sylabis} was left untouched.${RESET}"
    else
        say "Nothing to uninstall."
    fi
    exit 0
}

case "${1:-}" in
    --uninstall|uninstall) uninstall ;;
    "" ) ;;
    * ) fail "unknown option: ${1} (only --uninstall is supported)" ;;
esac

# ---------- find python --------------------------------------------------

say ""
say "${BOLD}sylabis installer${RESET}"
say ""

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            PY="$(command -v "$candidate")"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    say "${RED}✗ Python 3.10 or newer is required and was not found.${RESET}" >&2
    say "" >&2
    say "  Install it, then re-run this script:" >&2
    case "$(uname -s)" in
        Darwin) say "    brew install python3" >&2 ;;
        Linux)
            say "    sudo apt install python3 python3-venv   # Debian/Ubuntu" >&2
            say "    sudo dnf install python3                # Fedora" >&2 ;;
        *)      say "    https://www.python.org/downloads/" >&2 ;;
    esac
    exit 1
fi
step "Using $("$PY" -V 2>&1) at $PY"

# ---------- create the venv ----------------------------------------------

mkdir -p "$DATA_DIR" "$BIN_DIR"

if [ ! -x "$VENV_DIR/bin/pip" ]; then
    step "Creating environment in $VENV_DIR"
    if ! "$PY" -m venv "$VENV_DIR" >/dev/null 2>&1; then
        rm -rf "$VENV_DIR"
        say "${RED}✗ Could not create a virtualenv.${RESET}" >&2
        if [ "$(uname -s)" = "Linux" ]; then
            say "  On Debian/Ubuntu the venv module is separate:" >&2
            say "    sudo apt install python3-venv" >&2
            say "  then re-run this script." >&2
        fi
        exit 1
    fi
else
    step "Reusing environment in $VENV_DIR"
fi

# ---------- install ------------------------------------------------------

step "Installing sylabis from ${SOURCE}"
if ! "$VENV_DIR/bin/pip" install --quiet --upgrade "$SOURCE"; then
    say "${DIM}pip failed — retrying with full output:${RESET}"
    "$VENV_DIR/bin/pip" install --upgrade "$SOURCE" || fail "pip install failed (full output above)."
fi

for name in sy sylabis; do
    [ -x "$VENV_DIR/bin/$name" ] || fail "expected $VENV_DIR/bin/$name after install, but it is missing"
    ln -sf "$VENV_DIR/bin/$name" "$BIN_DIR/$name"
done

# ---------- PATH ----------------------------------------------------------

on_path() {
    case ":$PATH:" in
        *":$BIN_DIR:"*) return 0 ;;
        *) return 1 ;;
    esac
}

PATH_HINT=""
if ! on_path; then
    EXPORT_LINE="export PATH=\"$BIN_DIR:\$PATH\""
    shell_name="$(basename "${SHELL:-sh}")"
    case "$shell_name" in
        zsh)  rc="$HOME/.zshrc" ;;
        bash) rc="$HOME/.bashrc" ;;
        fish) rc="" ;;
        *)    rc="$HOME/.profile" ;;
    esac
    if [ "$shell_name" = "fish" ]; then
        fish_conf_dir="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d"
        mkdir -p "$fish_conf_dir"
        printf 'fish_add_path --global %s\n' "$BIN_DIR" > "$fish_conf_dir/sylabis.fish"
        step "Added $BIN_DIR to PATH via $fish_conf_dir/sylabis.fish"
    elif [ -n "$rc" ] && ! grep -sq "sylabis installer" "$rc"; then
        {
            printf '\n# Added by the sylabis installer\n'
            printf '%s\n' "$EXPORT_LINE"
        } >> "$rc"
        step "Added $BIN_DIR to PATH in $rc"
    fi
    PATH_HINT="  Open a new terminal (or run: ${BOLD}${EXPORT_LINE}${RESET}) first."
fi

# ---------- first-run setup ----------------------------------------------
# Offer to capture the API key right here — the one step between "installed"
# and "learning". Reads from /dev/tty so it works under `curl | sh`; piped
# installs with no terminal fall through silently to the `sy init` hint.

KEY_SAVED=0
ENV_FILE="${SYLABIS_HOME:-$HOME/sylabis}/.env"
if [ -z "${ANTHROPIC_API_KEY:-}" ] && ! grep -sq '^ANTHROPIC_API_KEY=' "$ENV_FILE" && [ -r /dev/tty ]; then
    printf '%s' "  Paste your Anthropic API key (console.anthropic.com/settings/keys), or Enter to skip: "
    stty -echo < /dev/tty 2>/dev/null || true
    IFS= read -r KEY < /dev/tty || KEY=""
    stty echo < /dev/tty 2>/dev/null || true
    printf '\n'
    # Absolute installed path — the freshly-linked `sy` may not be on PATH yet.
    if [ -n "$KEY" ] && "$VENV_DIR/bin/sy" init --key "$KEY"; then
        KEY_SAVED=1
    fi
fi

# ---------- done ----------------------------------------------------------

VERSION="$("$VENV_DIR/bin/python" -c 'from importlib.metadata import version; print(version("sylabis"))' 2>/dev/null || echo "unknown")"

say ""
ok "sylabis ${VERSION} installed."
say ""
say "  Start the agent:  ${BOLD}sy${RESET}"
[ -n "$PATH_HINT" ] && say "$PATH_HINT"
say ""
if [ "$KEY_SAVED" = 0 ]; then
    say "  ${DIM}One step left: run \`sy init\` to save your Anthropic API key.${RESET}"
fi
say "  ${DIM}Upgrade any time by re-running this installer.${RESET}"
say ""
