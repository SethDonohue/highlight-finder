#!/usr/bin/env bash
# Highlight Finder installer for macOS (Apple silicon or Intel).
#
#   curl -fsSL https://raw.githubusercontent.com/SethDonohue/highlight-finder/main/install.sh | bash
#
# Installs Homebrew (if missing), ffmpeg and Python 3.12, then the newest Highlight Finder release into
# its own folder, and puts `hlf` on your PATH. Safe to run again: that's also how you update.
# Your working folder (~/HighlightFinder) and your decisions are never touched.
set -euo pipefail

REPO="SethDonohue/highlight-finder"
APP_DIR="${HLF_APP_DIR:-$HOME/.highlight-finder}"
VENV="$APP_DIR/venv"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\n\033[1mStopped:\033[0m %s\n' "$*" >&2; exit 1; }

# Test hooks (not needed on the Mac): HLF_SKIP_BREW=1 HLF_PYTHON=python3 HLF_BIN_DIR=... HLF_WHEEL=path
if [ -z "${HLF_SKIP_BREW:-}" ]; then
  [ "$(uname)" = "Darwin" ] || fail "This installer is for macOS."

  if ! command -v brew >/dev/null 2>&1; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$b" ] && eval "$("$b" shellenv)" && break
    done
  fi
  if ! command -v brew >/dev/null 2>&1; then
    step "1/4  Installing Homebrew (it will ask for your Mac password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" </dev/tty
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$b" ] && eval "$("$b" shellenv)" && break
    done
    command -v brew >/dev/null 2>&1 || fail "Homebrew didn't install. Install it from https://brew.sh, then run this again."
  else
    step "1/4  Homebrew is installed"
  fi
  # So brew's programs (and hlf) are found in every new Terminal window.
  line="eval \"\$($(command -v brew) shellenv)\""
  touch "$HOME/.zprofile"
  grep -qF "$line" "$HOME/.zprofile" || printf '\n%s\n' "$line" >> "$HOME/.zprofile"

  step "2/4  Installing ffmpeg and Python 3.12 (skipped if already there)"
  brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
  brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
  PY="$(brew --prefix)/bin/python3.12"
  BIN_DIR="$(brew --prefix)/bin"
else
  step "1-2/4  Skipping Homebrew (test mode)"
  PY="${HLF_PYTHON:-python3}"
  BIN_DIR="${HLF_BIN_DIR:-$HOME/.local/bin}"
fi

step "3/4  Installing the newest Highlight Finder"
[ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip

WHEEL="${HLF_WHEEL:-}"
if [ -z "$WHEEL" ]; then
  # Public repo: ask GitHub for the newest release's wheel.
  url="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
    | "$VENV/bin/python" -c 'import json,sys
for a in json.load(sys.stdin).get("assets", []):
    if a["name"].endswith(".whl"): print(a["browser_download_url"]); break' 2>/dev/null || true)"
  if [ -n "$url" ]; then
    WHEEL="$TMP/$(basename "$url")"
    curl -fsSL -o "$WHEEL" "$url"
  else
    # Private repo: use the GitHub command line, signed in as you.
    echo "Couldn't download the release directly (is the repo private?). Using the GitHub command line instead."
    command -v gh >/dev/null 2>&1 || brew install gh
    gh auth status >/dev/null 2>&1 || gh auth login </dev/tty
    gh release download -R "$REPO" -p '*.whl' -D "$TMP"
    WHEEL="$(ls "$TMP"/*.whl | head -1)"
  fi
fi
[ -f "$WHEEL" ] || fail "No release file found to install."
"$VENV/bin/python" -m pip install --quiet --upgrade "$WHEEL"

step "4/4  Putting hlf on your PATH"
mkdir -p "$BIN_DIR"
ln -sf "$VENV/bin/hlf" "$BIN_DIR/hlf"
"$VENV/bin/hlf" --version
"$VENV/bin/hlf" doctor || true

step "Done. Open a new Terminal window and type:  hlf"
echo "It will walk you through your first trip. To update later, run the same install line again."
