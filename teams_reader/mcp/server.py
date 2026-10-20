"""Thin MCP stdio server exposing the store tools to Claude.

Kept intentionally small: all logic lives in ``teams_reader.mcp.tools`` and the
store. Importing this module requires the ``mcp`` package; the tool logic does
not, so it stays unit-testable on its own.
"""
from __future__ import annotations

import functools
import threading
import time
from pathlib import Path

from teams_reader import config, logs, refresh, settings
from teams_reader.broker import client as broker_client
from teams_reader.mcp import freshness, livesync, tools
from teams_reader.store import db as db_mod


def _server_class():
    """Return the MCP server class across SDK versions (v1 FastMCP, v2 MCPServer)."""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2.0
        return MCPServer
    except ModuleNotFoundError:
        from mcp.server.fastmcp import FastMCP  # mcp < 2.0
        return FastMCP


def _annotations(read_only: bool):
    from mcp.types import ToolAnnotations

    return ToolAnnotations(readOnlyHint=read_only, destructiveHint=False, openWorldHint=not read_only)


def _image_class():
    """Return the MCP Image helper across SDK versions."""
    try:
        from mcp.server.mcpserver import Image  # mcp >= 2.0
    except ModuleNotFoundError:
        from mcp.server.fastmcp import Image  # mcp < 2.0
    return Image


def instructions(profile_name: str) -> str:
    return (
        f"Read-only access to the user's own Microsoft Teams chats (profile {profile_name!r}); "
        "it never sends, reacts or marks anything read. Chats change constantly: before answering "
        "anything about the current state of a chat (what's new, the latest message, whether someone "
        "replied, what the user should reply), call read_chat or recent again, even when earlier "
        "results are in the conversation. Never answer those from earlier tool output. Results carry "
        "`freshness`: `as_of`, `web_sync_age_s`, and `web_sync_error` when only the desktop cache "
        "(which can lag minutes) is current; mention it when it matters. Drafts from suggest_reply are "
        "for the user to copy and send; never claim a message was sent."
    )


_TRANSCRIPT_SYNC_KEY = "transcripts_synced_at"
_TRANSCRIPT_SYNC_WINDOW_S = 120.0


def _recent_transcript_sync(conn) -> int | None:
    """Seconds since the last transcript sync when it is inside the window."""
    raw = db_mod.get_state(conn, _TRANSCRIPT_SYNC_KEY)
    if not raw:
        return None
    age = time.time() - float(raw)
    return round(age) if age < _TRANSCRIPT_SYNC_WINDOW_S else None


def _broker_live(profile: settings.Profile) -> broker_client.LiveProxy:
    client = broker_client.BrokerClient(
        broker_client.socket_path(profile.data_dir),
        spawn=lambda: broker_client.spawn_broker(profile.name),
    )
    return broker_client.LiveProxy(client)


def build_server(db_path: Path | None = None):
    config.ensure_app_dirs()
    db_file = db_path or config.db_path()
    conn = db_mod.connect(db_file)
    profile = settings.active()
    server = _server_class()("teams-reader", instructions=instructions(profile.name))
    log = logs.get("mcp")
    log.info("server ready (store %s, live sync %s)", db_file, "on" if livesync.enabled() else "off")
    register = server.tool

    def logged_tool(read_only: bool = True, **options):
        options.setdefault("annotations", _annotations(read_only))

        def decorate(fn):
            @functools.wraps(fn)
            def call(*args, **kwargs):
                start = time.monotonic()
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    log.warning("%s(%s) failed after %.1fs: %s", fn.__name__,
                                logs.short_args(args, kwargs), time.monotonic() - start, exc)
                    raise
                log.info("%s(%s) -> %s in %.2fs", fn.__name__, logs.short_args(args, kwargs),
                         logs.summarize(result), time.monotonic() - start)
                return result
            return register(**options)(call)
        return decorate

    server.tool = logged_tool

    # Tool calls run on worker threads and share one sqlite connection.
    lock = threading.Lock()
    # The shared broker's browser serves background sync, images and links (None = one-shot).
    live = _broker_live(profile) if livesync.enabled() else None

    def _await_fresh() -> None:
        """Let the browser holder (any session) sync before answering."""
        from teams_reader.browser import profile_lock

        asked = time.time()
        with lock:
            freshness.mark_used(conn, asked)
        if live is None:
            return
        try:
            live.touch(fresh=True)
        except Exception as exc:  # noqa: BLE001 - answer from the store; freshness reports the gap
            log.warning("could not reach the browser broker: %s", exc)
            return
        waiter = db_mod.connect(db_file)  # sleeps without holding the shared connection
        try:
            outcome = freshness.wait_fresh(waiter, asked, syncing=profile_lock.is_held(profile.data_dir))
        finally:
            waiter.close()
        if outcome != "fresh":
            log.info("freshness before answering: %s (%.1fs)", outcome, time.time() - asked)

    def _query(fn, *args, fresh: bool = False, **kwargs):
        """Run a store tool; ``fresh`` tools answer about a chat's current state
        and so trigger (and briefly wait for) a web sync first."""
        if fresh:
            _await_fresh()
        with lock:
            refresh.maybe_refresh(conn, fingerprint_fn=refresh.work_cache_fingerprint)
            result = fn(conn, *args, **kwargs)
            if isinstance(result, dict):
                result = {**result, "freshness": freshness.report(conn)}
            return result

    @server.tool()
    def list_chats() -> list[dict]:
        """List Teams chats, newest first. Untitled chats are named after their
        participants (`named` is false for those)."""
        return _query(tools.list_chats, fresh=True)

    @server.tool()
    def read_chat(name_or_id: str, since: int | None = None, limit: int = 200) -> dict:
        """Read a chat by name (partial, or a participant's name) or id. Without
        `since`: the latest `limit` messages, oldest first; with `since` (ms): the
        messages after it. Reports `total` and `truncated`."""
        return _query(tools.read_chat, name_or_id, since=since, limit=limit, fresh=True)

    @server.tool()
    def search(
        query: str,
        limit: int = 50,
        chat: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> list[dict]:
        """Full-text search (any word matches, best first). Optionally limit to one
        chat (name or id) and a time window `since`/`until` (ms epoch)."""
        return _query(tools.search, query, limit=limit, chat=chat, since=since, until=until)

    @server.tool()
    def recent(since: int = 0, limit: int = 100) -> list[dict]:
        """Newest messages since a timestamp (ms)."""
        return _query(tools.recent, since=since, limit=limit, fresh=True)

    @server.tool()
    def ask(question: str, limit: int = 30) -> dict:
        """Return messages relevant to a question for the model to answer over."""
        return _query(tools.ask, question, limit=limit)

    @server.tool()
    def suggest_reply(name_or_id: str, intent: str | None = None, limit: int = 15) -> dict:
        """Context (recent thread + participant profiles + chat kind) for writing
        adaptive reply options to copy into a chat."""
        return _query(tools.suggest_reply, name_or_id, intent=intent, limit=limit, fresh=True)

    @server.tool()
    def chat_profile(name_or_id: str) -> dict:
        """Heuristic profile of a chat and its participants (language, tone, length)."""
        return _query(tools.chat_profile, name_or_id)

    @server.tool()
    def list_images(name_or_id: str) -> dict:
        """List image messages in a chat (msg_id, sender, ts, url). read_image fetches the pixels."""
        return _query(tools.list_images, name_or_id)

    @server.tool()
    def list_links(name_or_id: str | None = None, host: str | None = None) -> dict:
        """Links shared in a chat (all chats if omitted), newest first, with full
        URLs (Teams truncates long links in the text). Filter by `host`."""
        return _query(tools.list_links, name_or_id, host=host)

    @server.tool()
    def read_link(url: str, max_chars: int = 20000) -> dict:
        """Readable text of a link shared in a chat (Confluence, GitLab, docs),
        opened read-only in the reader's VPN browser. Only URLs from list_links
        are allowed. Reports `needs_login` when the site wants a one-time login."""
        from teams_reader.browser import links as links_mod

        def is_shared(link: str) -> bool:
            with lock:  # the page load itself runs unlocked
                return db_mod.link_shared(conn, link)

        def fetch_page(link: str) -> dict:
            if live is None:
                return links_mod.fetch_page(link)
            return live.job("read_link", url=link)

        return tools.read_link(url, fetch_page, is_shared, max_chars=max_chars)

    @server.tool(structured_output=False)
    def read_image(msg_id: str) -> list:
        """Return a message's image(s) inline, downloading via the Teams session
        first. The local copy is deleted right after, so nothing lingers on disk.
        Needs a prior login."""
        from teams_reader.browser import images as images_mod

        def download(_conn, mid):
            # The browser download can take a minute: release the lock so other
            # tools keep answering, and write through a separate connection.
            lock.release()
            try:
                if live is not None:
                    return live.job("images", msg_ids=[mid])
                own = db_mod.connect(db_file)
                try:
                    return images_mod.download_images(own, msg_ids=[mid], headless=True)
                finally:
                    own.close()
            finally:
                lock.acquire()

        with lock:
            out = tools.consume_images(conn, msg_id, download)
        if "images" not in out:
            return [f"error: {out['error']}" + (f" ({out['hint']})" if "hint" in out else "")]
        image_cls = _image_class()
        return [image_cls(data=data, format=fmt) for data, fmt in out["images"]]

    @server.tool()
    def list_transcripts(chat: str | None = None, refresh: bool = True) -> dict:
        """Meeting transcripts (newest first) from recorded or transcribed Teams
        meetings and group/1:1 calls: title, start, duration, speakers. With
        `refresh`, first looks for new ones in all chats and downloads them (seconds on a warm
        browser). read_transcript returns the text."""
        from teams_reader.browser import transcripts as transcripts_mod

        chat_id = None
        if chat:
            with lock:
                chat_id = tools.resolve_chat(conn, chat)
            if chat_id is None:
                return {"error": f"chat not found: {chat}"}
        synced: dict | None = None
        with lock:
            recently = _recent_transcript_sync(conn)
        if refresh and recently is not None:
            synced = {"skipped": f"synced {recently}s ago; pass refresh=true again later or sync_history"}
        elif refresh:
            try:
                if live is not None:
                    synced = live.job("transcripts", chat_id=chat_id)
                else:
                    own = db_mod.connect(db_file)
                    try:
                        synced = transcripts_mod.sync_once(own, chat_id)
                    finally:
                        own.close()
            except Exception as exc:  # noqa: BLE001 - still list what is stored
                synced = {"error": str(exc), "hint": "Run `teams-reader login` and ensure the VPN is up."}
            else:
                with lock:
                    db_mod.set_state(conn, _TRANSCRIPT_SYNC_KEY, repr(time.time()))
        with lock:
            listed = tools.list_transcripts(conn, chat_id)
        return {"transcripts": listed, "refresh": synced}

    @server.tool()
    def read_transcript(transcript_id: int, max_chars: int = 60000) -> dict:
        """A meeting transcript as speaker turns, `[mm:ss] Name: text`, one per
        line. Get ids from list_transcripts."""
        with lock:
            return tools.read_transcript(conn, transcript_id, max_chars=max_chars)

    @server.tool(read_only=False)
    def sync_history(max_pages: int = 50, chat: str | None = None, full: bool = False) -> dict:
        """Sync from the Teams server now: real chat titles, new chats, and new
        messages (only chats with new activity unless `full`). A background sync
        already runs every ~30s while the tools are in use, so call this only to
        force it (e.g. a just-sent message). The first call starts the browser
        (~30s); later ones take seconds. Needs a prior `teams-reader login` + VPN."""
        from teams_reader.browser import backfill as backfill_mod

        try:
            if live is not None:
                live.touch()
                summary = live.sync(chat_filter=chat, full=full, max_pages=max_pages)
            else:
                own = db_mod.connect(db_file)
                try:
                    summary = backfill_mod.backfill(
                        own, max_pages=max_pages, chat_filter=chat, headless=True, full=full
                    )
                finally:
                    own.close()
        except Exception as exc:  # noqa: BLE001 - surface the reason to the model
            return {
                "error": str(exc),
                "hint": "Run `teams-reader login` once and ensure the VPN (SOCKS5) is up.",
            }
        result = {
            "chats_synced": summary.chats_synced,
            "new_messages": summary.new_messages,
            "total_messages": summary.total_messages,
        }
        if summary.failed_chats:
            result["failed_chats"] = summary.failed_chats
            result["last_error"] = summary.last_error
        if live is not None:
            result["background"] = live.status()
        return result

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
