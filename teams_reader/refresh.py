"""Debounced incremental refresh so MCP queries always see the latest cache.

Each read tool calls ``maybe_refresh`` first; it runs an incremental ingest at
most once per ``min_interval`` seconds and never raises (a query must still work
if the cache is momentarily unreadable). With a ``fingerprint_fn`` the ~5s
dump is skipped entirely while the cache files are unchanged.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from teams_reader.cache import locate
from teams_reader.cache.accounts import MixedCacheError
from teams_reader.ingest import ingest
from teams_reader.store import db as db_mod

_FINGERPRINT_KEY = "cache_fingerprint"
# Bump when parsing/storage changes so an unchanged cache is re-ingested once.
PARSER_VERSION = 5

DEFAULT_MIN_INTERVAL = 3.0

_state: dict[str, object] = {"last": 0.0, "fingerprint": None, "leveldb": None}


def reset() -> None:
    _state["last"] = 0.0
    _state["fingerprint"] = None
    _state["leveldb"] = None


def leveldb_fingerprint(leveldb: Path | None) -> str | None:
    """Size+mtime of the files dfindexeddb reads (.ldb tables and MANIFEST)."""
    if leveldb is None or not leveldb.is_dir():
        return None
    entries = []
    for item in leveldb.iterdir():
        if item.suffix == ".ldb" or item.name.startswith("MANIFEST-"):
            stat = item.stat()
            entries.append((item.name, stat.st_size, stat.st_mtime_ns))
    return json.dumps([PARSER_VERSION, sorted(entries)])


def work_cache_fingerprint(locator: Callable[[], Path | None] = locate.active_leveldb) -> str | None:
    # Locating walks the whole WebView tree (~200ms), so remember the hit.
    leveldb = _state["leveldb"]
    if not isinstance(leveldb, Path) or not leveldb.is_dir():
        leveldb = locator()
        _state["leveldb"] = leveldb
    return leveldb_fingerprint(leveldb)


def maybe_refresh(
    conn: sqlite3.Connection,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    now_fn: Callable[[], float] = time.monotonic,
    ingest_fn: Callable[[sqlite3.Connection], object] = ingest,
    fingerprint_fn: Callable[[], str | None] | None = None,
) -> bool:
    """Run an incremental ingest unless one ran within ``min_interval`` or the
    cache fingerprint is unchanged since the last successful ingest.

    Returns True if a refresh ran. Swallows all ingest errors.
    """
    if os.environ.get("TEAMS_READER_NO_REFRESH"):
        return False
    now = now_fn()
    if _state["last"] and now - _state["last"] < min_interval:
        return False
    _state["last"] = now
    try:
        fingerprint = fingerprint_fn() if fingerprint_fn is not None else None
        if fingerprint is not None and fingerprint == _last_fingerprint(conn):
            return False
        try:
            ingest_fn(conn)
        except MixedCacheError:
            pass  # remember the fingerprint anyway: a web sync clears it (set_web_chats)
        if fingerprint is not None:
            _state["fingerprint"] = fingerprint
            if conn is not None:
                db_mod.set_state(conn, _FINGERPRINT_KEY, fingerprint)
    except Exception:  # noqa: BLE001 - a failed refresh must not break the query
        return False
    return True


def _last_fingerprint(conn: sqlite3.Connection | None) -> str | None:
    # Read from the store (not memory) so restarts skip the cold ingest and a
    # wiped store forces one.
    if conn is not None:
        return db_mod.get_state(conn, _FINGERPRINT_KEY)
    return _state["fingerprint"]
