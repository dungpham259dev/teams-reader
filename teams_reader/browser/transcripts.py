"""Find and download meeting transcripts through the logged-in Teams session.

``discover_with`` pages each meeting chat on chatsvc (the recording cards are
system messages the message sync drops) and records the transcript URLs.
``download_with`` fetches them from AMS with the browser's cookies, like images.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Callable

from teams_reader.browser import chatsvc
from teams_reader.parse import transcripts as parse
from teams_reader.store import db as db_mod

_FIRST_SCAN_PAGES = 10
# New recording cards land on the newest pages; a rescan need not walk the history.
_RESCAN_PAGES = 2
_SCANNED_PREFIX = "transcripts_scanned:"
_GONE = {404, 410}
_AUTH_ERRORS = {401, 403}


def _meeting_chats(conn: sqlite3.Connection, chat_id: str | None) -> list[str]:
    ids = [r["chat_id"] for r in db_mod.list_chats(conn) if parse.is_meeting_chat(r["chat_id"])]
    return [c for c in ids if chat_id is None or c == chat_id]


def _scan_chat(conn: sqlite3.Connection, fetch: Callable[[str], object], template: str, chat_id: str) -> int:
    scanned = db_mod.get_state(conn, _SCANNED_PREFIX + chat_id) is not None
    url: str | None = chatsvc.with_conversation(template, chat_id)
    added, read_pages = 0, 0
    for _ in range(_RESCAN_PAGES if scanned else _FIRST_SCAN_PAGES):
        payload = fetch(url)
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            break
        read_pages += 1
        added += sum(db_mod.add_transcript_ref(conn, chat_id, ref) for ref in parse.extract_refs(payload["messages"]))
        url = chatsvc.backward_link(payload)
        if not url:
            break
    if read_pages:  # a failed scan stays a first scan, so its history is walked next time
        db_mod.set_state(conn, _SCANNED_PREFIX + chat_id, str(int(time.time() * 1000)))
    return added


def discover_with(
    conn: sqlite3.Connection,
    fetch: Callable[[str], object],
    template: str,
    chat_id: str | None = None,
) -> int:
    """Record transcripts posted in meeting chats; returns how many are new."""
    return sum(_scan_chat(conn, fetch, template, cid) for cid in _meeting_chats(conn, chat_id))


class _NetworkDown(Exception):
    """A request failed outright; the rest of the batch would wait out the same timeout."""


def _download_one(conn: sqlite3.Connection, context: Any, url: str) -> str | None:
    """The transcript's new status, or None to retry it later."""
    try:
        resp = context.request.get(url, timeout=90_000)
    except Exception as exc:  # noqa: BLE001 - e.g. the VPN dropped
        raise _NetworkDown(str(exc)) from exc
    if resp.status in _AUTH_ERRORS:
        from teams_reader.browser.live import AuthExpired

        raise AuthExpired(f"transcript fetch returned {resp.status}")
    now = int(time.time() * 1000)
    if resp.status in _GONE:
        db_mod.set_transcript(conn, url, "unavailable", now)
        return "unavailable"
    body = resp.body().decode("utf-8", errors="replace") if resp.status == 200 else ""
    if not body.lstrip("﻿").startswith("WEBVTT"):
        return None  # e.g. an HTML sign-in page served with 200
    cues = parse.parse_vtt(body)
    db_mod.set_transcript(conn, url, "ok", now, text=parse.render(cues), speakers=parse.cue_speakers(cues))
    return "ok"


def download_with(conn: sqlite3.Connection, context: Any, limit: int = 5) -> dict[str, int]:
    """Fetch pending transcripts through an open, logged-in browser context."""
    urls = db_mod.pending_transcripts(conn, limit=limit)
    outcomes: list[str | None] = []
    for url in urls:
        try:
            outcomes.append(_download_one(conn, context, url))
        except _NetworkDown:
            break
    return {
        "requested": len(urls),
        "downloaded": outcomes.count("ok"),
        "unavailable": outcomes.count("unavailable"),
    }


def sync_with(conn: sqlite3.Connection, session: Any, chat_id: str | None = None) -> dict[str, int]:
    """Discover then download, on an open reader session (``live.PlaywrightSession``)."""
    found = discover_with(conn, session.fetch, session.template_url, chat_id)
    return {"new": found, **download_with(conn, session.context)}


def sync_once(conn: sqlite3.Connection, chat_id: str | None = None) -> dict[str, int]:
    """One-shot sync in its own browser, for when no warm browser is running."""
    from teams_reader.browser.live import PlaywrightSession

    session = PlaywrightSession(interactive=True)
    try:
        return sync_with(conn, session, chat_id)
    finally:
        session.close()
