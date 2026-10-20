"""The browser jobs a broker client may ask for, by name.

Each job runs on the broker's browser thread as ``job(session, conn, args)``
and returns plain JSON data, since the result crosses the socket.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Callable

from teams_reader.browser import backfill, images, links, transcripts
from teams_reader.mcp import freshness
from teams_reader.store import db as db_mod

_FRESH_MAX_PAGES = 20

Job = Callable[[Any, sqlite3.Connection, dict[str, Any]], Any]


def _sync(session: Any, conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    summary = backfill.sync_with(
        conn, session.fetch, session.template_url,
        chat_filter=args.get("chat_filter"),
        full=bool(args.get("full", False)),
        max_pages=int(args.get("max_pages", backfill.DEFAULT_MAX_PAGES)),
    )
    return dataclasses.asdict(summary)


def fresh_sync(session: Any, conn: sqlite3.Connection, started: float) -> dict[str, Any]:
    """An incremental sync for a question that wants current data."""
    summary = _sync(session, conn, {"max_pages": _FRESH_MAX_PAGES})
    freshness.mark_synced(conn, started)
    return summary


def _transcripts(session: Any, conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, int]:
    return transcripts.sync_with(conn, session, args.get("chat_id"))


def _images(session: Any, conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, int]:
    return images.download_with(
        conn, session.context, msg_ids=[str(m) for m in args.get("msg_ids") or []],
        headers=getattr(session, "api_headers", None),
    )


def _read_link(session: Any, conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get("url") or "")
    # The socket is local, but the browser carries the user's cookies: open only shared links.
    if not db_mod.link_shared(conn, url):
        return {"error": "link was not shared in any chat"}
    return links.fetch_page_with(session.context, url)


JOBS: dict[str, Job] = {
    "sync": _sync,
    "transcripts": _transcripts,
    "images": _images,
    "read_link": _read_link,
}
