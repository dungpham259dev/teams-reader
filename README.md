# teams-reader

Read your own Microsoft Teams chats with Claude. `teams-reader` copies chats
from the new Teams (v2) desktop app's local cache, and optionally from your
Teams web session, into a searchable local SQLite store, and serves them to
Claude Code over MCP. No Microsoft Graph, no admin consent, macOS only.

It only reads: it never sends messages, never marks them as read and never
reports presence. Nothing leaves your Mac except the requests to Teams itself.

## Install

You need macOS, the new Teams desktop app signed in, and
[Homebrew](https://brew.sh). Close Claude Code, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/dungpham259dev/teams-reader/main/install.sh | bash
```

The installer sets up the CLI and its browser, then runs `teams-reader setup`.
Setup asks a few questions (profile name, Teams cache, VPN proxy, optional
automatic sign-in, background sync) and then finishes everything:

1. reads the Teams desktop cache;
2. opens a window to sign in to Teams web (approve MFA on your phone) and
   downloads every chat's full history with real chat names;
3. registers the MCP server with Claude Code.

It ends with a ✓/✗ summary. A skipped or failed step prints the command that
finishes it later. Re-run `teams-reader setup` any time to add or change a
profile.

<details>
<summary>Manual install</summary>

```bash
brew install snappy uv
export CPPFLAGS="-I$(brew --prefix snappy)/include" LDFLAGS="-L$(brew --prefix snappy)/lib"
uv tool install --python-preference only-managed --python 3.13 \
  "teams-reader[browser] @ git+https://github.com/dungpham259dev/teams-reader"
teams-reader install-browser
teams-reader setup
```

The compiler flags are needed because a dependency pins `python-snappy` 0.6.1,
which builds against Homebrew's snappy.
</details>

## Use with Claude

Open Claude Code and ask, for example:

- "Which Teams chats do I have?"
- "Summarize the Project Sigma chat since this morning."
- "Find messages about keycloak last week."
- "What are the images in that chat about?"
- "Suggest a reply to the last message from Anna."

| Tool | What it does |
|------|--------------|
| `list_chats`, `read_chat`, `recent` | browse chats and their latest messages |
| `search`, `ask` | full-text search, filterable by chat and date |
| `chat_profile`, `suggest_reply` | participants' style, reply suggestions to copy (never sent) |
| `list_images`, `read_image` | see a chat's images; the downloaded file is deleted after reading |
| `list_links`, `read_link` | links shared in chats, and the page text behind them |
| `sync_history` | pull new or full history from Teams web on demand |

## Profiles

Settings live in `~/.config/teams-reader/config.toml` as named profiles; see
`config.example.toml`. Each profile has its own store, browser sign-in and
images, so accounts never mix.

```bash
teams-reader profiles          # list profiles (* = default)
teams-reader use client-b      # make client-b the default
teams-reader config            # show the active profile (never secrets)
teams-reader --profile client-b backfill   # one command on another profile
```

The `teams-reader` MCP server follows the default profile: after
`teams-reader use`, run `/mcp` in Claude Code (or restart it) to switch.
To use several accounts side by side instead, run `teams-reader setup` for
each one. Setup registers a separate server per extra profile
(`teams-<name>`), and Claude can query all of them at once.

The desktop app keeps every work account in one cache, so a second work
account should use `cache = "none"` and get its chats from Teams web only.

## How it works

**Desktop cache.** New Teams stores chats in a Chromium IndexedDB (LevelDB)
cache. `teams-reader` snapshots it so Teams can keep running, decodes it with
`dfindexeddb`, and upserts the messages into SQLite with an FTS5 index. Each
MCP call first re-reads the cache if it changed, so answers include the
latest cached messages.

**Teams web.** The cache holds only recent messages and often no chat names.
A Playwright browser signed in to Teams web reads the same internal `chatsvc`
API the web app uses, page by page, for full history, edits, deletions and
real chat names. It blocks every request that would mark messages read or
report presence, and parks on a blank page instead of opening a chat. Set the
profile's `proxy` if Teams is only reachable through a VPN.

**Warm browser.** While Claude is using the tools, the MCP server keeps one
headless browser open and syncs changed chats every ~30s, so new messages show
up quickly and `sync_history`, `read_image` and `read_link` take seconds. It
closes after 15 idle minutes. Turn it off with `live_sync = false`. A profile's
browser serves one process at a time, so CLI browser commands report "in use"
while the MCP server holds it.

**Automatic sign-in.** If the profile names a `credentials_env` dotenv file
(with `email_key`/`password_key`), an expired session is signed in again during
actions you start. MFA still needs your phone, and the number shows as a macOS
notification. A rejected password is never retried. Background sync never
signs in; it reports `login required` instead.

## CLI

```bash
teams-reader setup              # add or change a profile, sign in, register with Claude
teams-reader profiles | use NAME | config
teams-reader login              # sign in to Teams web (opens a window)
teams-reader backfill [--full] [--chat NAME] [--max-pages N]
teams-reader ingest | daemon    # read the desktop cache once / every 45s
teams-reader fetch-image --chat NAME --limit N
teams-reader open-link URL      # sign in to a linked site once, for read_link
teams-reader locate             # list the Teams caches found
teams-reader mcp                # the MCP server (stdio)
teams-reader wipe --yes         # delete the store and snapshots
```

## Privacy and limits

- The store is a local copy of chat content that may belong to a client;
  decide per tenant whether that is acceptable. It lives under
  `~/Library/Application Support/teams-reader/`; `teams-reader wipe --yes`
  deletes it.
- No admin rights are needed. If reading the cache fails with "Operation not
  permitted", give your terminal Full Disk Access (System Settings → Privacy &
  Security) and restart it.
- Tenant policies can block the cache: Conditional Access may leave it empty
  on unmanaged devices, and Intune app protection may encrypt it. Check with
  `teams-reader locate` and `teams-reader ingest`.

## Development

```bash
python3 -m venv venv && venv/bin/pip install -e '.[browser,dev]'
venv/bin/pytest --cov=teams_reader
```

Tests use synthetic fixtures only; no real chat data is committed. Try
automatic sign-in safely in a throwaway profile with
`venv/bin/python tools/try_autologin.py`.

## License

MIT, see `LICENSE`.
