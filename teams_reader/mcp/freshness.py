"""Freshness shared by every MCP server process on one profile.

Each Claude Code session starts its own ``teams-reader mcp``; they share the
store but only one can hold the warm browser. A question in any session marks
``tool_used_at``; the browser holder sees it and syncs right away, and the
asking process waits briefly for that sync so answers include messages sent
seconds ago.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any, Callable

from teams_reader.store import db as db_mod

USED_KEY = "tool_used_at"
SYNCED_KEY = "web_synced_at"
ERROR_KEY = "web_sync_error"
FRESH_S = 15.0
WAIT_S = 8.0
_STEP_S = 0.5


def _float(conn: sqlite3.Connection, key: str) -> float | None:
    raw = db_mod.get_state(conn, key)
    return float(raw) if raw else None


def mark_used(conn: sqlite3.Connection, at: float) -> None:
    db_mod.set_state(conn, USED_KEY, repr(at))


def used_at(conn: sqlite3.Connection) -> float | None:
    return _float(conn, USED_KEY)


def mark_synced(conn: sqlite3.Connection, at: float) -> None:
    db_mod.set_state(conn, SYNCED_KEY, repr(at))
    db_mod.delete_state(conn, ERROR_KEY)


def mark_failed(conn: sqlite3.Connection, error: str) -> None:
    db_mod.set_state(conn, ERROR_KEY, error[:300])


def synced_at(conn: sqlite3.Connection) -> float | None:
    return _float(conn, SYNCED_KEY)


def wait_fresh(
    conn: sqlite3.Connection,
    used_at: float,
    syncing: bool,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Wait up to ``WAIT_S`` for a web sync newer than ``used_at``.

    ``syncing`` says a warm browser exists (this process or another). Never
    waits when web sync is failing or absent, so answers stay instant then.
    """
    last = synced_at(conn)
    if last is not None and used_at - last <= FRESH_S:
        return "fresh"
    if not syncing or db_mod.get_state(conn, ERROR_KEY):
        return "not syncing"
    while now() - used_at < WAIT_S:
        sleep(_STEP_S)
        last = synced_at(conn)
        if last is not None and last >= used_at:
            return "synced"
    return "stale"


def report(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    """How current the store is, for tool results."""
    now = time.time() if now is None else now
    last = synced_at(conn)
    out: dict[str, Any] = {"as_of": datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")}
    if last is None:
        out["web_sync"] = "never"
    else:
        out["web_sync_age_s"] = round(now - last)
    error = db_mod.get_state(conn, ERROR_KEY)
    if error:
        out["web_sync_error"] = error
    return out
