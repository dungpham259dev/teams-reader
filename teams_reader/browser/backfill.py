"""Full-history backfill over chatsvc (Hướng B).

``collect_messages`` is the pure pagination loop (injectable ``fetch``, unit-
tested). ``backfill`` drives a real logged-in browser: it captures one live
messages request (URL + auth headers), then replays it per conversation,
following backward links, and upserts everything into the store.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable

from teams_reader.browser import chatsvc
from teams_reader.parse.messages import Message
from teams_reader.store import db as db_mod

Fetch = Callable[[str], object]

DEFAULT_MAX_PAGES = 50


@dataclass(frozen=True)
class ChatBackfill:
    chat_id: str | None
    pages: int
    messages: list[Message] = field(default_factory=list)
    # True only when the needed history was fully read (no page cap, no fetch error).
    complete: bool = False
    error: str | None = None


def collect_messages(
    first_url: str,
    fetch: Fetch,
    chat_id: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    stop_at_ts: int | None = None,
) -> ChatBackfill:
    """Page backward from ``first_url`` via ``fetch`` until history is exhausted,
    or until a page reaches ``stop_at_ts`` (history already stored)."""
    messages: list[Message] = []
    url: str | None = first_url
    pages = 0
    complete = False
    error: str | None = None
    while url and pages < max_pages:
        payload = fetch(url)
        pages += 1
        if payload is None or (isinstance(payload, dict) and "error" in payload):
            error = "empty response" if payload is None else str(payload["error"])
            break
        page = chatsvc.parse_page(payload, chat_id=chat_id)
        messages.extend(page)
        if stop_at_ts is not None and any(m.ts is not None and m.ts <= stop_at_ts for m in page):
            complete = True
            break
        url = chatsvc.backward_link(payload)
        complete = url is None
    return ChatBackfill(chat_id=chat_id, pages=pages, messages=messages, complete=complete, error=error)


@dataclass(frozen=True)
class BackfillSummary:
    chats_synced: int
    new_messages: int
    total_messages: int
    failed_chats: int = 0
    last_error: str | None = None


@dataclass(frozen=True)
class Target:
    chat_id: str
    name: str
    stop_at_ts: int | None


def plan_targets(
    conversations: list[chatsvc.Conversation],
    watermarks: dict[str, int],
    names: dict[str, str] | None = None,
    chat_filter: str | None = None,
    full: bool = False,
) -> list[Target]:
    """Chats worth fetching: new or active since their watermark (all if ``full``)."""
    names = names or {}
    needle = (chat_filter or "").casefold()
    targets: list[Target] = []
    for conv in conversations:
        name = conv.topic or names.get(conv.chat_id) or conv.chat_id
        if needle and needle not in name.casefold() and chat_filter != conv.chat_id:
            continue
        mark = watermarks.get(conv.chat_id)
        if full or mark is None:
            targets.append(Target(conv.chat_id, name, None))
        elif conv.last_activity_ms is not None and conv.last_activity_ms > mark:
            targets.append(Target(conv.chat_id, name, mark))
    return targets


def apply_conversations(
    conn: sqlite3.Connection, conversations: list[chatsvc.Conversation]
) -> None:
    """Store server titles and kinds (a missing title keeps the cached one)."""
    for conv in conversations:
        db_mod.set_chat_meta(conn, conv.chat_id, conv.topic, conv.kind)
    conn.commit()
    if conversations:
        db_mod.set_web_chats(conn, [conv.chat_id for conv in conversations])


def _chat_targets(
    conn: sqlite3.Connection, chat_filter: str | None
) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for row in db_mod.list_chats(conn):
        chat_id = row["chat_id"]
        if not chat_id:
            continue
        name = row["display_name"] or chat_id
        if chat_filter and chat_filter not in name and chat_filter != chat_id:
            continue
        targets.append((chat_id, name))
    return targets


def sync_with(
    conn: sqlite3.Connection,
    fetch: Fetch,
    template_url: str,
    *,
    chat_filter: str | None = None,
    full: bool = False,
    max_pages: int = DEFAULT_MAX_PAGES,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> BackfillSummary:
    """Sync chats through ``fetch`` (an authenticated chatsvc GET).

    The server's conversation list supplies real titles/kinds and each chat's
    last activity, so only new or changed chats are fetched (``full`` refetches
    all), and paging stops once already-stored history is reached.
    """
    from teams_reader import logs

    log = logs.get("sync")
    added = 0
    synced = 0
    failed = 0
    last_error: str | None = None
    for target, activity in _targets(conn, fetch, template_url, chat_filter, full):
        first = chatsvc.with_conversation(template_url, target.chat_id)
        result = collect_messages(
            first, fetch, chat_id=target.chat_id,
            max_pages=max_pages, stop_at_ts=target.stop_at_ts,
        )
        added += db_mod.upsert_messages(conn, result.messages, source="browser")
        # A capped or failed read leaves the watermark, so the gap is retried.
        if result.complete:
            db_mod.set_chat_watermark(conn, target.chat_id, activity or 0)
        if result.error is not None:
            failed += 1
            last_error = result.error
            log.warning("chat %s: page %d failed (%s); retried next sync", target.name, result.pages, result.error)
        synced += 1
        if on_progress is not None:
            on_progress(target.name, result.pages, len(result.messages))
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    log.debug("synced %d chats: %d new, %d stored", synced, added, total)
    return BackfillSummary(chats_synced=synced, new_messages=added, total_messages=total,
                           failed_chats=failed, last_error=last_error)


def backfill(
    conn: sqlite3.Connection,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    chat_filter: str | None = None,
    headless: bool = False,
    timeout_ms: int = 180_000,
    full: bool = False,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> BackfillSummary:
    """One-shot sync: launch the browser, capture a live request, :func:`sync_with`.

    Requires a logged-in persistent profile (``teams-reader login``). Imports
    Playwright lazily so the core package stays import-light.
    """
    from playwright.sync_api import sync_playwright

    from teams_reader.browser import session

    with sync_playwright() as pw:
        context = session.launch_context(pw, headless=headless)
        try:
            from teams_reader.browser.autologin import make_autologin

            template_url, headers = session.capture_request(
                context, timeout_ms=timeout_ms, autologin=make_autologin(), can_prompt=not headless
            )
            session.park(context.pages[0] if context.pages else context.new_page())
            return sync_with(
                conn, lambda url: session.fetch_api(context, url, headers), template_url,
                chat_filter=chat_filter, full=full, max_pages=max_pages, on_progress=on_progress,
            )
        finally:
            context.close()


def _targets(
    conn: sqlite3.Connection,
    fetch: Fetch,
    template_url: str,
    chat_filter: str | None,
    full: bool,
) -> list[tuple[Target, int | None]]:
    """Plan from the server conversation list; fall back to the stored chats."""
    conversations = chatsvc.parse_conversations(fetch(chatsvc.conversations_url(template_url)))
    if not conversations:
        return [(Target(cid, name, None), None) for cid, name in _chat_targets(conn, chat_filter)]
    apply_conversations(conn, conversations)
    names = {r["chat_id"]: r["display_name"] for r in db_mod.list_chats(conn) if r["display_name"]}
    activity = {c.chat_id: c.last_activity_ms for c in conversations}
    plan = plan_targets(
        conversations, db_mod.chat_watermarks(conn), names=names,
        chat_filter=chat_filter, full=full,
    )
    return [(t, activity.get(t.chat_id)) for t in plan]
