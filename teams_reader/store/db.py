"""SQLite-backed message store.

Messages are inserted idempotently (``INSERT OR IGNORE`` on ``msg_id``) so
re-ingesting the same cache never duplicates or mutates existing rows. An FTS5
index mirrors the ``messages`` table through triggers for search.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Sequence

from teams_reader.parse.messages import Message

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: the MCP server runs tool calls on worker threads
    # while holding one shared connection. Writes stay serialised through the
    # single connection, so cross-thread reads are safe here.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL: the background sync connection writes while tool calls read.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    purge_non_messages(conn)
    return conn


_ADDED_COLUMNS = {
    "version": "INTEGER",
    "edited": "INTEGER NOT NULL DEFAULT 0",
    "deleted": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a store was created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    for name, ddl in _ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")
    conn.commit()


def purge_non_messages(conn: sqlite3.Connection) -> int:
    """Drop rows stored before the parser filtered system/activity records."""
    cur = conn.execute(
        # GLOB is case-sensitive, matching the parser's exact prefixes.
        "DELETE FROM messages WHERE sender GLOB 'http://*' "
        "OR sender GLOB 'https://*' OR sender GLOB 'worker/*'"
    )
    if cur.rowcount:
        conn.execute("DELETE FROM attachments WHERE msg_id NOT IN (SELECT msg_id FROM messages)")
        conn.execute("DELETE FROM links WHERE msg_id NOT IN (SELECT msg_id FROM messages)")
        _refresh_chats(conn)
    # Activity streams (48:mentions, ...) are not chats; notes is the self-chat.
    conn.execute(
        "DELETE FROM chats WHERE chat_id GLOB '48:*' AND chat_id != '48:notes' "
        "AND chat_id NOT IN (SELECT DISTINCT chat_id FROM messages WHERE chat_id IS NOT NULL)"
    )
    conn.commit()
    return cur.rowcount


def upsert_messages(
    conn: sqlite3.Connection, messages: Iterable[Message], source: str = "cache"
) -> int:
    """Insert new messages; return how many message rows were actually added."""
    messages = list(messages)
    rows = [
        (
            m.msg_id, m.chat_id, m.sender, m.ts, m.text, m.quoted_text, source,
            getattr(m, "version", None), int(getattr(m, "edited", False)),
            int(getattr(m, "deleted", False)),
        )
        for m in messages
    ]
    attachments = [
        (m.msg_id, url)
        for m in messages
        for url in getattr(m, "image_urls", ())
    ]
    before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    # A newer server version (edit/delete) replaces the content; a stale copy
    # (e.g. the cache before compaction) never rolls it back. Missing ts is filled.
    newer = "excluded.version > COALESCE(messages.version, 0)"
    conn.executemany(
        "INSERT INTO messages "
        "(msg_id, chat_id, sender, ts, text, quoted_text, source, version, edited, deleted) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(msg_id) DO UPDATE SET "
        "ts = COALESCE(messages.ts, excluded.ts), "
        f"text = CASE WHEN {newer} THEN excluded.text ELSE messages.text END, "
        f"quoted_text = CASE WHEN {newer} THEN excluded.quoted_text ELSE messages.quoted_text END, "
        f"edited = CASE WHEN {newer} THEN excluded.edited ELSE messages.edited END, "
        f"deleted = CASE WHEN {newer} THEN excluded.deleted ELSE messages.deleted END, "
        f"version = CASE WHEN {newer} THEN excluded.version ELSE messages.version END "
        f"WHERE (messages.ts IS NULL AND excluded.ts IS NOT NULL) OR {newer}",
        rows,
    )
    if attachments:
        conn.executemany(
            "INSERT OR IGNORE INTO attachments (msg_id, url) VALUES (?, ?)",
            attachments,
        )
    links = [(m.msg_id, url) for m in messages for url in getattr(m, "link_urls", ())]
    if links:
        conn.executemany("INSERT OR IGNORE INTO links (msg_id, url) VALUES (?, ?)", links)
    after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    _refresh_chats(conn)
    conn.commit()
    return after - before


def _refresh_chats(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO chats (chat_id, last_msg_ts) "
        "SELECT chat_id, MAX(ts) FROM messages WHERE chat_id IS NOT NULL AND deleted = 0 GROUP BY chat_id "
        "ON CONFLICT(chat_id) DO UPDATE SET last_msg_ts = excluded.last_msg_ts"
    )


def set_chat_name(conn: sqlite3.Connection, chat_id: str, display_name: str, kind: str | None = None) -> None:
    conn.execute(
        "INSERT INTO chats (chat_id, display_name, kind) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET display_name = excluded.display_name, "
        "kind = COALESCE(excluded.kind, chats.kind)",
        (chat_id, display_name, kind),
    )
    conn.commit()


def set_chat_meta(
    conn: sqlite3.Connection, chat_id: str, display_name: str | None, kind: str | None
) -> None:
    """Upsert a chat's title/kind; a None field keeps the stored value."""
    conn.execute(
        "INSERT INTO chats (chat_id, display_name, kind) VALUES (?, ?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET "
        "display_name = COALESCE(excluded.display_name, chats.display_name), "
        "kind = COALESCE(excluded.kind, chats.kind)",
        (chat_id, display_name, kind),
    )


_WATERMARK_PREFIX = "chat_wm:"


def chat_watermarks(conn: sqlite3.Connection) -> dict[str, int]:
    """Per chat, the server activity time up to which it has been synced."""
    rows = conn.execute(
        "SELECT key, value FROM sync_state WHERE key LIKE ?", (_WATERMARK_PREFIX + "%",)
    ).fetchall()
    return {r["key"][len(_WATERMARK_PREFIX):]: int(r["value"]) for r in rows}


def set_chat_watermark(conn: sqlite3.Connection, chat_id: str, ts: int) -> None:
    set_state(conn, _WATERMARK_PREFIX + chat_id, str(ts))


def list_chats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.chat_id, c.display_name, c.kind, c.last_msg_ts, "
        "COUNT(m.msg_id) AS message_count "
        "FROM chats c LEFT JOIN messages m ON m.chat_id = c.chat_id AND m.deleted = 0 "
        "GROUP BY c.chat_id ORDER BY c.last_msg_ts DESC"
    ).fetchall()


def read_chat(
    conn: sqlite3.Connection,
    chat_id: str,
    since_ts: int | None = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND deleted = 0 AND (? IS NULL OR ts > ?) "
        "ORDER BY ts LIMIT ?",
        (chat_id, since_ts, since_ts, limit),
    ).fetchall()


def tail_chat(conn: sqlite3.Connection, chat_id: str, limit: int = 15) -> list[sqlite3.Row]:
    """The most recent messages of a chat, in chronological order."""
    # Unknown-ts rows sort as oldest so stale ones never crowd out the latest.
    rows = conn.execute(
        "SELECT * FROM messages WHERE chat_id = ? AND deleted = 0 "
        "ORDER BY ts IS NULL, ts DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return list(reversed(rows))


def count_chat(conn: sqlite3.Connection, chat_id: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ? AND deleted = 0", (chat_id,)).fetchone()[0]


def chat_senders(conn: sqlite3.Connection) -> dict[str, list[tuple[str, int]]]:
    """Per chat, its senders with message counts, most active first."""
    out: dict[str, list[tuple[str, int]]] = {}
    for row in conn.execute(
        "SELECT chat_id, sender, COUNT(*) AS n FROM messages WHERE deleted = 0 "
        "GROUP BY chat_id, sender ORDER BY chat_id, n DESC, sender"
    ):
        out.setdefault(row["chat_id"], []).append((row["sender"], row["n"]))
    return out


def sender_texts(conn: sqlite3.Connection, chat_id: str, per_sender: int = 60) -> dict[str, list[str]]:
    """Recent message texts grouped by sender, for profiling."""
    rows = conn.execute(
        "SELECT sender, text FROM messages WHERE chat_id = ? AND deleted = 0 ORDER BY ts DESC",
        (chat_id,),
    ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        bucket = out.setdefault(row["sender"], [])
        if len(bucket) < per_sender:
            bucket.append(row["text"])
    return out


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    chat_id: str | None = None,
    since_ts: int | None = None,
    until_ts: int | None = None,
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.* FROM messages_fts f JOIN messages m ON m.rowid = f.rowid "
        "WHERE messages_fts MATCH ? AND m.deleted = 0 "
        "AND (? IS NULL OR m.chat_id = ?) AND (? IS NULL OR m.ts >= ?) AND (? IS NULL OR m.ts <= ?) "
        "ORDER BY rank LIMIT ?",
        (query, chat_id, chat_id, since_ts, since_ts, until_ts, until_ts, limit),
    ).fetchall()


def recent(conn: sqlite3.Connection, since_ts: int, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE ts > ? AND deleted = 0 ORDER BY ts DESC LIMIT ?",
        (since_ts, limit),
    ).fetchall()


def images_for_chat(conn: sqlite3.Connection, chat_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT a.msg_id, a.url, a.local_path, m.ts, m.sender, m.text "
        "FROM attachments a JOIN messages m ON m.msg_id = a.msg_id "
        "WHERE m.chat_id = ? AND m.deleted = 0 ORDER BY m.ts",
        (chat_id,),
    ).fetchall()


def image_row(conn: sqlite3.Connection, msg_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT a.msg_id, a.url, a.local_path, m.chat_id, m.ts, m.sender "
        "FROM attachments a JOIN messages m ON m.msg_id = a.msg_id "
        "WHERE a.msg_id = ? LIMIT 1",
        (msg_id,),
    ).fetchone()


def link_shared(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM links WHERE url = ? LIMIT 1", (url,)).fetchone() is not None


def links_for(conn: sqlite3.Connection, chat_id: str | None = None) -> list[sqlite3.Row]:
    """Links shared in a chat (or all chats), newest first."""
    return conn.execute(
        "SELECT l.url, l.msg_id, m.chat_id, m.sender, m.ts "
        "FROM links l JOIN messages m ON m.msg_id = l.msg_id "
        "WHERE m.deleted = 0 AND (? IS NULL OR m.chat_id = ?) ORDER BY m.ts DESC",
        (chat_id, chat_id),
    ).fetchall()


def images_for_msg(conn: sqlite3.Connection, msg_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT msg_id, url, local_path FROM attachments WHERE msg_id = ? ORDER BY rowid",
        (msg_id,),
    ).fetchall()


def pending_images(
    conn: sqlite3.Connection,
    chat_filter: str | None = None,
    msg_ids: list[str] | None = None,
    limit: int = 50,
) -> list[tuple[str, str]]:
    """(msg_id, url) pairs whose bytes are not downloaded yet."""
    sql = (
        "SELECT a.msg_id, a.url FROM attachments a "
        "JOIN messages m ON m.msg_id = a.msg_id "
        "LEFT JOIN chats c ON c.chat_id = m.chat_id "
        "WHERE a.local_path IS NULL"
    )
    params: list[object] = []
    if chat_filter:
        sql += " AND (m.chat_id = ? OR c.display_name LIKE ?)"
        params += [chat_filter, f"%{chat_filter}%"]
    if msg_ids:
        placeholders = ",".join("?" for _ in msg_ids)
        sql += f" AND a.msg_id IN ({placeholders})"
        params += list(msg_ids)
    sql += " ORDER BY m.ts DESC LIMIT ?"
    params.append(limit)
    return [(r["msg_id"], r["url"]) for r in conn.execute(sql, params).fetchall()]


def set_image_path(conn: sqlite3.Connection, msg_id: str, url: str, local_path: str) -> None:
    conn.execute(
        "UPDATE attachments SET local_path = ? WHERE msg_id = ? AND url = ?",
        (local_path, msg_id, url),
    )
    conn.commit()


def clear_image_path(conn: sqlite3.Connection, msg_id: str, url: str) -> None:
    conn.execute(
        "UPDATE attachments SET local_path = NULL WHERE msg_id = ? AND url = ?",
        (msg_id, url),
    )
    conn.commit()


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def high_water_ts(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(ts) AS mx FROM messages").fetchone()
    return row["mx"] or 0


def clear_all(conn: sqlite3.Connection) -> None:
    """Remove every row (messages, chats, state) and its FTS mirror."""
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM chats")
    conn.execute("DELETE FROM attachments")
    conn.execute("DELETE FROM links")
    conn.execute("DELETE FROM sync_state")
    conn.commit()
