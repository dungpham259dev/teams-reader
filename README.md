# teams-reader

Read your own Microsoft Teams chats from the new Teams (v2) desktop app's local
cache (and, optionally, the Teams web session) into a searchable SQLite store,
and expose them to Claude over MCP — no Microsoft Graph, no admin consent.
macOS only. Each user reads only their own chats.

It reads only your own local Teams data. Nothing is uploaded by the tool itself.

## How it works

The new Teams app stores chat in a Chromium IndexedDB/LevelDB cache. `teams-reader`:

1. **locate** the work (`teams.microsoft.com`) LevelDB under the Teams container.
2. **snapshot** it (copy, drop `LOCK`) so Teams can stay running.
3. **dump** it to JSONL with `dfindexeddb`.
4. **parse** messages (sender, time, text; reply quotes split out) and chat names.
5. **store** them in SQLite with an FTS5 index, idempotently (re-ingest never duplicates).
6. serve tools to Claude via **MCP**: `list_chats`, `read_chat`, `search`,
   `recent`, `ask`, `chat_profile`, and `suggest_reply`.

Each MCP tool first runs a debounced incremental ingest, so queries always see
the latest cached messages without running `ingest`/`daemon` by hand.

`suggest_reply(chat)` returns the recent thread, the chat kind, and a heuristic
profile of each participant (language, tone, message length) — Claude uses that
to write 2-3 adaptive reply options to copy into the chat. It never sends.

## Images

Messages reference images by URL on the AMS service; the bytes need the
logged-in session. `teams-reader` records image URLs per message during ingest,
then downloads the bytes through the same browser context used for backfill
(cookie auth, no CORS). The model reads the downloaded file.

```bash
teams-reader fetch-image --chat "Project Sigma" --limit 10
```

Then, over MCP: `list_images("Project Sigma")` to see a chat's images, and
`read_image(msg_id)` to get the image inline; the downloaded file is deleted right after it is read.

## Full history (browser backfill)

The cache holds only recently synced messages. To pull a chat's **full history**,
`teams-reader` can read it from the Teams web client's internal `chatsvc` messages
API, reusing your own logged-in session — no Graph, no admin consent.

It drives a real browser (Playwright/Chromium) optionally through a proxy (e.g. a corporate VPN),
captures one live `chatsvc` request (URL + session token), then replays it per
conversation and follows the backward-pagination links until history is exhausted.
It only reads; nothing is sent.

```bash
pip install -e '.[browser]'
python -m playwright install chromium

# If the profile uses a VPN proxy, bring it up first.
teams-reader login                 # one-time: a window opens, log in (MFA)
teams-reader backfill              # sync new/changed chats (real titles from the server)
teams-reader backfill --full       # refetch every chat's history
teams-reader backfill --chat Sigma --max-pages 100
```

The login persists, so later `backfill` runs (and the MCP `sync_history` tool) can
reuse the session headless. Set the profile's `proxy` if Teams is only reachable
through a VPN proxy.

## Warm browser (MCP server)

The MCP server keeps one headless reader browser open after the first tool
call. While the tools are in use it re-syncs changed chats every ~30s in the
background (backing off when the VPN or login is unavailable), and
`sync_history`, `read_image` and `read_link` reuse it, so they take seconds
instead of ~30s. Disable it with `live_sync = false` in the profile. While it
runs it holds that profile's browser, so CLI browser commands for the same
profile report "in use" — stop the MCP server first.

## Unattended re-login

When the Teams session has expired, user-initiated actions (`sync_history`,
`read_image`, `read_link`, CLI `login`/`backfill`/`fetch-image`) sign in again by
themselves, using the profile's `credentials_env` dotenv file and its
`email_key`/`password_key` (no file configured = never signs in by itself;
disable with `autologin = false`). MFA still
needs your phone: the number to enter shows as a macOS notification. A rejected
password is never retried. The background sync never signs in, so it cannot
spam MFA prompts; it reports `login required` instead.
Try it safely in a throwaway profile: `venv/bin/python tools/try_autologin.py`.

## Requirements

- macOS, the new Teams app signed into the target account and synced at least once.
- `brew install snappy` (the LevelDB reader needs it).
- Python 3.10+.

## Install

Needs macOS, Python 3.11+, the new Teams desktop app, and Homebrew.

```bash
brew install snappy uv
uv tool install "teams-reader[browser] @ git+https://github.com/dungpham259dev/teams-reader"
teams-reader install-browser   # Chromium for the browser features
```

The repo is private: ask for access, and make sure git can authenticate to
GitHub (e.g. `gh auth login`) before installing.

From a checkout instead: `python3 -m venv venv && venv/bin/pip install -e ".[browser]"`.

## Configuration

Settings live in `~/.config/teams-reader/config.toml` as named **profiles**
(see `config.example.toml`): local cache source, optional proxy (VPN), optional
credentials file for unattended re-login, background sync on/off, data folder.
Without a config file the `default` profile reads the work cache, connects
directly and never signs in by itself.

```bash
teams-reader config                     # show the active profile (no secrets)
teams-reader --profile client-b config  # another profile
```

Each profile keeps its own store and browser login, and a browser profile can
be used by one process at a time (a second one fails fast with "in use").
Register one MCP server per profile so accounts never mix:

```bash
claude mcp add teams-reader -- teams-reader mcp                          # default_profile
claude mcp add teams-client-b -- teams-reader --profile client-b mcp
```

## Use

```bash
teams-reader config           # show the active profile's settings
teams-reader locate           # show the Teams cache databases found
teams-reader ingest           # read the cache into the store (one shot)
teams-reader daemon           # keep the store fresh (polls every 45s)
teams-reader login            # one-time browser login for full-history backfill
teams-reader backfill         # sync new/changed chats via the browser session
teams-reader fetch-image      # download chat images via the browser session
teams-reader mcp              # run the MCP server (stdio) for Claude
teams-reader wipe --yes       # delete the store and snapshots
```

Register the MCP server with Claude Code (see Configuration for profiles):

```bash
claude mcp add teams-reader -- teams-reader mcp
```

Then ask Claude things like "tóm tắt chat Project Sigma" or "tìm tin nhắn về keycloak".

## Permissions (macOS)

Cache files live in your own user container, so no admin/sudo is needed. If a run
can't read them ("Operation not permitted"), grant your terminal **Full Disk
Access** (System Settings → Privacy & Security → Full Disk Access) and restart it.

## Data governance

This stores a local copy of chat content, which may belong to a client. Decide
per tenant whether that is acceptable. `teams-reader wipe --yes` removes the store
and all snapshots. The store lives at
`~/Library/Application Support/teams-reader/`.

## Tenant limits

The cache approach depends on the tenant: Conditional Access can block sync on
unmanaged devices (empty cache), and Intune app-protection can encrypt the cache
(unreadable). Try `teams-reader locate` and `teams-reader ingest` on a tenant before relying on it.

## Development

```bash
PYTHONPATH=. venv/bin/pytest --cov=teams_reader
```

Tests use synthetic fixtures only — no real chat data is committed.

## License

MIT, see `LICENSE`.
