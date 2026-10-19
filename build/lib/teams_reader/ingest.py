"""Ingest pipeline: LevelDB snapshot -> dfindexeddb dump -> parse -> store.

The low-level steps are injectable so the whole pipeline can be exercised in
tests without touching the real Teams cache.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from teams_reader import settings
from teams_reader.cache import accounts as accounts_mod
from teams_reader.cache import dump as dump_mod
from teams_reader.cache import locate as locate_mod
from teams_reader.cache import snapshot as snapshot_mod
from teams_reader.parse.conversations import resolve_names
from teams_reader.parse.decode import decode_stream, unwrap
from teams_reader.parse.messages import extract_messages
from teams_reader.store import db as db_mod

Locator = Callable[[], "Path | None"]


@dataclass(frozen=True)
class IngestResult:
    new_messages: int
    total_messages: int
    chats_named: int
    last_ts: int


def ingest_from_dump(
    conn: sqlite3.Connection, dump_path: Path, account: str | None = None
) -> IngestResult:
    """Parse a dfindexeddb JSONL dump and upsert it into the store.

    A cache shared by several accounts is narrowed to this profile's account
    (see :mod:`teams_reader.cache.accounts`).
    """
    raw = Path(dump_path).read_text(encoding="utf-8")
    records = [r for r in decode_stream(raw) if isinstance(r, dict)]
    chosen = accounts_mod.resolve(conn, records, account)
    if chosen:
        records = accounts_mod.records_for(records, chosen)
    values = [unwrap(r["value"]) for r in records if "value" in r]

    messages = extract_messages(values)
    new_count = db_mod.upsert_messages(conn, messages)

    # Only name chats that actually have messages, so member/roster records
    # (which also carry ids + display names) do not pollute the chat list.
    known = {row["chat_id"] for row in db_mod.list_chats(conn) if row["chat_id"]}
    names = resolve_names(values, known_ids=known)
    named = 0
    for chat_id, (display_name, kind) in names.items():
        db_mod.set_chat_name(conn, chat_id, display_name, kind)
        named += 1

    last_ts = db_mod.high_water_ts(conn)
    db_mod.set_state(conn, "last_sync_ts", str(last_ts))
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return IngestResult(
        new_messages=new_count,
        total_messages=total,
        chats_named=named,
        last_ts=last_ts,
    )


def ingest(
    conn: sqlite3.Connection,
    locator: Locator = locate_mod.active_leveldb,
    work_dir: Path | None = None,
    snapshot_fn: Callable[[Path, Path], Path] = snapshot_mod.snapshot_leveldb,
    dump_fn: Callable[[Path, Path], Path] = dump_mod.dump_to_jsonl,
) -> IngestResult:
    """Run the full pipeline against the located work LevelDB."""
    leveldb = locator()
    if leveldb is None:
        raise FileNotFoundError(
            "No Teams message cache for this profile (cache disabled, or the account "
            "is not signed into the desktop Teams app and synced)."
        )
    work = work_dir if work_dir is not None else settings.active().work_dir
    work.mkdir(parents=True, exist_ok=True)
    snap = snapshot_fn(leveldb, work / "snapshot")
    dump_path = dump_fn(snap, work / "dump.jsonl")
    return ingest_from_dump(conn, dump_path, account=settings.active().account)
