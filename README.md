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

### Install with Claude Code

You can also ask Claude Code: "Install teams-reader from
https://github.com/dungpham259dev/teams-reader, follow its README". Claude's
shell has no terminal for the questions, so it runs setup unattended:

1. Ask the user, in one message: a profile name (default `default`); is
   Teams reachable only through a VPN proxy, and its URL; optionally, a
   `.env` file and the variable names holding the Teams email and password,
   for automatic re-login (never read or print the values).
2. Run, with a 10-minute timeout, adding only the options the user gave:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/dungpham259dev/teams-reader/main/install.sh | bash -s -- \
     --yes --name NAME --proxy socks5://127.0.0.1:1080 \
     --credentials-env ~/path/.env --email-key EMAIL --password-key PASSWORD
   ```

   Tell the user first that a browser window will open to sign in to Teams
   (approve MFA on the phone), then downloads the chat history and closes.
3. Relay the setup summary, then ask the user to run `/mcp` (or restart
   Claude Code) so the new server loads.

`teams-reader setup --help` lists every option (`--cache`, `--no-sign-in`,
`--no-live-sync`, `--make-default`, ...).

### Update

```bash
teams-reader update
```

It pulls the latest version and its browser, and keeps your config, chats and
sign-in, and stops the running browser broker so the next request starts one
on the new code. Then run `/mcp` in each open Claude Code session (or restart
it).
Installs from before this command existed update once with:

```bash
CPPFLAGS="-I$(brew --prefix snappy)/include" LDFLAGS="-L$(brew --prefix snappy)/lib" \
  uv tool upgrade --reinstall-package teams-reader teams-reader && teams-reader install-browser
```

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
| `list_transcripts`, `read_transcript` | transcripts of recorded or transcribed meetings and calls, as speaker turns |
| `sync_history` | pull new or full history from Teams web on demand; reports `failed_chats` and `last_error` when some chats could not be read |

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

The desktop app keeps every work account in one cache. When it holds
messages for more than one account, each profile reads only its own: after
the Teams web sign-in, teams-reader matches the cache to the account by its
chats and remembers it. Until then it reads nothing from that cache (Claude
is told why), so accounts never mix. To pin it by hand, set
`account = "<user id>"` in the profile; the ids are listed in that message.

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

**Warm browser.** One headless browser per profile is shared by every Claude
session: the first MCP server that needs it starts `teams-reader broker` in the
background, and all MCP servers send their browser work (`sync_history`,
`read_image`, `read_link`, transcripts) to it over a Unix socket in the profile
folder. Before answering about a chat's current state (`list_chats`,
`read_chat`, `recent`, `suggest_reply`) the MCP server has the broker sync
first, so a message sent seconds ago is included: about 3s with the browser
open, about 20s right after the broker started. Questions asked during one sync
share it. Between questions the broker syncs changed chats every ~30s. When its Teams session expires, the next
question that wants fresh data signs it in again (at most every 10 minutes).
The broker exits after 30 idle minutes and starts again on demand. Turn it off
with `live_sync = false`. CLI browser commands report "in use" while the broker
holds the browser. Behind a proxy, Teams' public script CDN
(`*.static.microsoft`, `*.cdn.office.net`) is fetched directly, so a first boot
after a Teams update does not download the whole app through a slow VPN; sign-in
and every API call still use the proxy. If it still outlasts the default wait
("Teams did not load"), set `load_timeout = 420` (seconds) in the profile.

**Meeting transcripts.** When a meeting or call is recorded or transcribed,
Teams posts a recording card that links its WebVTT transcript: in the meeting
chat for a scheduled meeting, in the group or 1:1 chat for a call started there.
`list_transcripts` pages those chats for new cards, downloads the
transcripts with the browser's cookies and keeps them in the store as
`[mm:ss] Name: text` turns. Teams expires old transcripts; those are listed as
`unavailable`. Transcripts of calls started in a group chat need the session's
auth header as well as its cookies; one still refused with 401 or 403 is listed
as `forbidden` and retried after 10 minutes.

**Automatic sign-in.** If the profile names a `credentials_env` dotenv file
(with `email_key`/`password_key`), an expired session is signed in again during
actions you start. MFA still needs your phone, and the number shows as a macOS
notification. A rejected password is never retried. Background sync never
signs in by itself; when it hits a sign-in page, the next question that wants
fresh data signs the broker in again (at most every 10 minutes).

## Activity log

Each profile keeps a log of what the reader does: tool calls with their
arguments, sizes and timings, desktop cache reads, the warm browser, background
sync, sign-in screens, and write requests it blocked. It never holds message
text, tokens or passwords. Follow it live with `teams-reader logs -f` (it lives
in the profile's data folder under `logs/`, rotated at 1 MB; set
`TEAMS_READER_LOG_LEVEL=DEBUG` for more detail).

## CLI

```bash
teams-reader setup              # add or change a profile, sign in, register with Claude
teams-reader profiles | use NAME | config
teams-reader logs -f            # follow the activity log live
teams-reader update             # latest version; keeps config and data
teams-reader login              # sign in to Teams web (opens a window)
teams-reader backfill [--full] [--chat NAME] [--max-pages N]
teams-reader ingest | daemon    # read the desktop cache once / every 45s
teams-reader fetch-image --chat NAME --limit N
teams-reader open-link URL      # sign in to a linked site once, for read_link
teams-reader locate             # list the Teams caches found
teams-reader mcp                # the MCP server (stdio)
teams-reader broker             # the shared browser (MCP servers start it on demand)
teams-reader wipe --yes         # delete the store and snapshots
```

## Privacy and limits

- The store is a local copy of chat content that may belong to a client;
  decide per tenant whether that is acceptable. It lives under
  `~/Library/Application Support/teams-reader/`; `teams-reader wipe --yes`
  deletes it. It stops the browser broker first and lists MCP servers that
  still hold the old store open; run `/mcp` in those sessions afterwards.
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
