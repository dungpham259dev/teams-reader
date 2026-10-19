#!/usr/bin/env bash
# One-command installer for teams-reader (macOS).
#   curl -fsSL https://raw.githubusercontent.com/dungpham259dev/teams-reader/main/install.sh | bash
# Unattended (e.g. run by Claude Code), arguments go to `teams-reader setup`:
#   curl -fsSL .../install.sh | bash -s -- --yes --cache work --proxy socks5://127.0.0.1:1080
# Installs dependencies and the CLI, downloads its browser, then runs the
# interactive `teams-reader setup` (config, cache read, Teams sign-in + first sync,
# Claude Code registration).
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
if [ "$#" -gt 0 ]; then
  teams-reader setup "$@"
elif [ -t 0 ]; then
  teams-reader setup
elif (exec </dev/tty) 2>/dev/null; then
  teams-reader setup </dev/tty  # piped from curl: answer from the terminal
else
  # No terminal (e.g. an agent's shell): -r /dev/tty passes there but opening it fails.
  say "No terminal to ask questions in. Finish with 'teams-reader setup' in a terminal,"
  say "or unattended: 'teams-reader setup --yes [--cache work] [--proxy URL] ...' (see --help)."
fi
