#!/usr/bin/env bash
# lcode installer for Linux and macOS (Apple Silicon).
#
#   curl -fsSL https://nasser1941.github.io/lcode/install.sh | bash
#
# What it does, step by step:
#   1. installs uv (https://docs.astral.sh/uv/) if missing; uv provides an isolated Python 3.12
#   2. installs lcode with `uv tool install` into ~/.local/bin
#   3. checks that Ollama is installed and running, and tells you how to install it if not
#   4. runs `lcode setup`, which picks the best model for your hardware and downloads it
#
# Environment overrides: LCODE_SOURCE (pip-style source to install from), LCODE_SKIP_SETUP=1.
set -euo pipefail

SOURCE="${LCODE_SOURCE:-git+https://github.com/nasser1941/lcode@main}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
    printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
    exit 1
}

os="$(uname -s)"
arch="$(uname -m)"
case "$os" in
Linux) platform=linux ;;
Darwin)
    platform=macos
    [ "$arch" = "arm64" ] || warn "Intel Macs are not supported well: models run on the CPU only."
    ;;
*) die "unsupported OS: $os. lcode runs on Linux, macOS and Windows via WSL2." ;;
esac

command -v curl >/dev/null || die "curl is required (Ubuntu: sudo apt install -y curl)"

if ! command -v uv >/dev/null; then
    info "Installing uv (Python package manager) from https://astral.sh"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

info "Installing lcode"
uv tool install --force --python 3.12 "$SOURCE"
if ! command -v lcode >/dev/null; then
    uv tool update-shell >/dev/null 2>&1 || true
    bin_dir="$(uv tool dir --bin)"
    export PATH="$bin_dir:$PATH"
fi
lcode --version

if ! command -v rg >/dev/null; then
    if [ "$platform" = linux ]; then
        warn "ripgrep is not installed; lcode will use a slower search. Install it: sudo apt install -y ripgrep"
    else
        warn "ripgrep is not installed; lcode will use a slower search. Install it: brew install ripgrep"
    fi
fi

ollama_url="${OLLAMA_HOST:-http://localhost:11434}"
case "$ollama_url" in *://*) ;; *) ollama_url="http://$ollama_url" ;; esac
if ! curl -fsS --max-time 3 "$ollama_url/api/version" >/dev/null 2>&1; then
    echo
    if command -v ollama >/dev/null; then
        bold "Ollama is installed but not running. Start it, then run: lcode setup"
        if [ "$platform" = linux ]; then
            echo "  sudo systemctl start ollama"
        else
            echo "  open -a Ollama   (or: brew services start ollama)"
        fi
    else
        bold "lcode needs Ollama (https://ollama.com) to run models. Install it, then run: lcode setup"
        if [ "$platform" = linux ]; then
            echo "  curl -fsSL https://ollama.com/install.sh | sh"
        else
            echo "  brew install ollama && brew services start ollama   (or download the app from https://ollama.com)"
        fi
    fi
    exit 0
fi

if [ "${LCODE_SKIP_SETUP:-0}" = 1 ]; then
    bold "Installed. Next: lcode setup"
elif [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
    info "Choosing and downloading a model for this machine"
    lcode setup </dev/tty
else
    bold "Installed. Next: lcode setup"
fi
