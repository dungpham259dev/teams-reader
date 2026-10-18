#!/usr/bin/env bash
# One-command installer for teams-reader (macOS).
#   curl -fsSL https://raw.githubusercontent.com/dungpham259dev/teams-reader/main/install.sh | bash
# Installs dependencies and the CLI, downloads its browser, then runs the
# interactive `teams-reader setup` (config profile + Claude Code registration).
set -euo pipefail

SOURCE="${TEAMS_READER_SOURCE:-git+https://github.com/dungpham259dev/teams-reader}"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "teams-reader supports macOS only."

if ! command -v brew >/dev/null 2>&1; then
  fail "Homebrew is required. Install it from https://brew.sh, then re-run this command."
fi

if ! brew list --versions snappy >/dev/null 2>&1; then
  say "Installing snappy (needed to read the Teams cache)"
  brew install snappy
fi

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv (Python tool manager)"
  brew install uv
fi

say "Installing teams-reader"
# dfindexeddb pins python-snappy 0.6.1, which compiles against Homebrew's snappy.
SNAPPY_PREFIX="$(brew --prefix snappy)"
export CPPFLAGS="-I${SNAPPY_PREFIX}/include ${CPPFLAGS:-}"
export LDFLAGS="-L${SNAPPY_PREFIX}/lib ${LDFLAGS:-}"
# A uv-managed Python avoids conda/system interpreters with odd build setups.
uv tool install --force --python-preference only-managed --python 3.13 "teams-reader[browser] @ ${SOURCE}"

BIN_DIR="$(uv tool dir --bin)"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    export PATH="$BIN_DIR:$PATH"
    say "Adding $BIN_DIR to your shell PATH"
    uv tool update-shell >/dev/null 2>&1 || true
    ;;
esac

say "Downloading the browser used for sync, images and links"
teams-reader install-browser

if [ "${TEAMS_READER_SKIP_SETUP:-0}" = "1" ]; then
  say "Installed. Run 'teams-reader setup' to configure."
  exit 0
fi

say "Setup"
if [ -t 0 ]; then
  teams-reader setup
elif [ -r /dev/tty ]; then
  teams-reader setup </dev/tty  # piped from curl: answer from the terminal
else
  say "No terminal available. Run 'teams-reader setup' to configure."
fi
