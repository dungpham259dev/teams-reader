"""Freshness shared by every MCP server process on one profile.

A question in any session marks ``tool_used_at`` (it keeps the broker's
background sync running) and asks the broker for a sync before answering; the
broker records each finished sync here so tool results can say how current the
store is.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from typing import Any

from teams_reader.store import db as db_mod

USED_KEY = "tool_used_at"
SYNCED_KEY = "web_synced_at"
ERROR_KEY = "web_sync_error"


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
