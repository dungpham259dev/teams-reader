"""Pure tool logic over the store, independent of the MCP transport.

Each function takes a connection and returns JSON-serialisable data, so the
tools can be unit-tested directly and the MCP server stays a thin wrapper.
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from teams_reader.parse.profile import profile_dict
from teams_reader.store import db as db_mod

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


_SPECIAL_CHATS = {"48:notes": "Notes (self)", "48:mentions": "Mentions"}
_SELF_CHAT = "48:notes"
_MAX_NAMED_PARTICIPANTS = 3


def _local_time(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000).astimezone().strftime("%Y-%m-%d %H:%M %z")


def _row_to_message(row: sqlite3.Row, names: dict[str, str]) -> dict[str, Any]:
    return {
        "msg_id": row["msg_id"],
        "chat_id": row["chat_id"],
        "chat_name": names.get(row["chat_id"], row["chat_id"]),
        "sender": row["sender"],
        "ts": row["ts"],
        "time": _local_time(row["ts"]),
        "text": row["text"],
        "quoted_text": row["quoted_text"],
        "edited": bool(row["edited"]),
    }


def _fallback_name(chat_id: str, senders: list[tuple[str, int]], me: str | None) -> str:
    """Name an untitled chat like Teams does: by its other participants."""
    if chat_id in _SPECIAL_CHATS:
        return _SPECIAL_CHATS[chat_id]
    others = [name for name, _ in senders if name != me]
    picked = others[:_MAX_NAMED_PARTICIPANTS] or [name for name, _ in senders[:1]]
    return ", ".join(picked) if picked else chat_id


def chat_names(conn: sqlite3.Connection) -> dict[str, str]:
    """chat_id -> display name, newest chat first; untitled chats get a fallback."""
    senders = db_mod.chat_senders(conn)
    self_senders = senders.get(_SELF_CHAT) or []
    me = self_senders[0][0] if self_senders else None
    return {
        r["chat_id"]: r["display_name"] or _fallback_name(r["chat_id"], senders.get(r["chat_id"], []), me)
        for r in db_mod.list_chats(conn)
    }


def _fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query (OR of quoted word tokens)."""
    tokens = _WORD_RE.findall(text or "")
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def resolve_chat(conn: sqlite3.Connection, name_or_id: str) -> str | None:
    """Resolve a chat id or a chat name: exact name, then a real title containing
    it, then a participant-derived name containing it (newest chat first)."""
    names = chat_names(conn)
    if name_or_id in names:
        return name_or_id
    needle = (name_or_id or "").casefold().strip()
    if not needle:
        return None
    titled = {r["chat_id"] for r in db_mod.list_chats(conn) if r["display_name"]}
    tiers = (
        lambda cid, name: name.casefold() == needle,
        lambda cid, name: cid in titled and needle in name.casefold(),
        lambda cid, name: needle in name.casefold(),
    )
    for matches in tiers:
        hit = next((cid for cid, name in names.items() if matches(cid, name)), None)
        if hit is not None:
            return hit
    return None


def list_chats(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    names = chat_names(conn)
    pending = db_mod.get_state(conn, "cache_account_pending")
    notice = [{"notice": f"Desktop cache not read: {pending}"}] if pending else []
    return notice + [
        {
            "chat_id": r["chat_id"],
            "display_name": names[r["chat_id"]],
            "named": bool(r["display_name"]),
            "kind": r["kind"],
            "last_msg_ts": r["last_msg_ts"],
            "last_msg_time": _local_time(r["last_msg_ts"]),
            "message_count": r["message_count"],
        }
        for r in db_mod.list_chats(conn)
    ]


def read_chat(
    conn: sqlite3.Connection,
    name_or_id: str,
    since: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Without ``since``: the latest ``limit`` messages. With it: messages after it."""
    chat_id = resolve_chat(conn, name_or_id)
    if chat_id is None:
        return {"error": f"chat not found: {name_or_id}", "messages": []}
    names = chat_names(conn)
    if since is None:
        rows = db_mod.tail_chat(conn, chat_id, limit=limit)
    else:
        rows = db_mod.read_chat(conn, chat_id, since_ts=since, limit=limit)
    total = db_mod.count_chat(conn, chat_id)
    return {
        "chat_id": chat_id,
        "chat_name": names.get(chat_id, chat_id),
        "total": total,
        "truncated": len(rows) < total if since is None else len(rows) == limit,
        "messages": [_row_to_message(r, names) for r in rows],
    }


def search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    chat: str | None = None,
    since: int | None = None,
    until: int | None = None,
) -> list[dict[str, Any]]:
    """Full-text search, optionally within one chat and a ts window (ms)."""
    fts = _fts_query(query)
    if fts is None:
        return []
    chat_id = None
    if chat:
        chat_id = resolve_chat(conn, chat)
        if chat_id is None:
            return []
    names = chat_names(conn)
    rows = db_mod.search(conn, fts, limit=limit, chat_id=chat_id, since_ts=since, until_ts=until)
    return [_row_to_message(r, names) for r in rows]


def recent(conn: sqlite3.Connection, since: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    names = chat_names(conn)
    return [_row_to_message(r, names) for r in db_mod.recent(conn, since_ts=since, limit=limit)]


def ask(conn: sqlite3.Connection, question: str, limit: int = 30) -> dict[str, Any]:
    """Return the messages most relevant to a question for Claude to answer over."""
    matches = search(conn, question, limit=limit)
    return {"question": question, "context": matches}


def chat_profile(conn: sqlite3.Connection, name_or_id: str) -> dict[str, Any]:
    """Heuristic profile of a chat and each participant (language, tone, length)."""
    chat_id = resolve_chat(conn, name_or_id)
    if chat_id is None:
        return {"error": f"chat not found: {name_or_id}"}
    participants = {
        sender: profile_dict(texts)
        for sender, texts in db_mod.sender_texts(conn, chat_id).items()
    }
    return {"chat_id": chat_id, "participants": participants}


def list_images(conn: sqlite3.Connection, name_or_id: str) -> dict[str, Any]:
    """Images in a chat: msg_id, sender, time, url."""
    chat_id = resolve_chat(conn, name_or_id)
    if chat_id is None:
        return {"error": f"chat not found: {name_or_id}", "images": []}
    rows = db_mod.images_for_chat(conn, chat_id)
    return {
        "chat_id": chat_id,
        "images": [
            {
                "msg_id": r["msg_id"],
                "sender": r["sender"],
                "ts": r["ts"],
                "time": _local_time(r["ts"]),
                "url": r["url"],
            }
            for r in rows
        ],
    }


_IMAGE_MAGIC = (
    (b"\xff\xd8", "jpeg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
)


def image_format(data: bytes) -> str | None:
    """Image format from magic bytes; None when the bytes are not an image."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((fmt for magic, fmt in _IMAGE_MAGIC if data.startswith(magic)), None)


def list_links(
    conn: sqlite3.Connection, name_or_id: str | None, host: str | None = None
) -> dict[str, Any]:
    """Links shared in a chat (or every chat when ``name_or_id`` is None)."""
    chat_id = None
    if name_or_id:
        chat_id = resolve_chat(conn, name_or_id)
        if chat_id is None:
            return {"error": f"chat not found: {name_or_id}", "links": []}
    names = chat_names(conn)
    links = []
    for r in db_mod.links_for(conn, chat_id):
        link_host = urlsplit(r["url"]).hostname or ""
        if host and host.casefold() not in link_host:
            continue
        links.append({
            "url": r["url"],
            "host": link_host,
            "chat_name": names.get(r["chat_id"], r["chat_id"]),
            "sender": r["sender"],
            "time": _local_time(r["ts"]),
            "msg_id": r["msg_id"],
        })
    return {"chat_id": chat_id, "links": links}


_LOGIN_MARKERS = ("id.atlassian.com/login", "login.microsoftonline.com", "/users/sign_in", "/login")
_DEFAULT_LINK_CHARS = 20_000


def read_link(
    url: str,
    fetch_page: Callable[[str], dict[str, Any]],
    is_shared: Callable[[str], bool],
    max_chars: int = _DEFAULT_LINK_CHARS,
) -> dict[str, Any]:
    """Readable text of a link shared in a chat (only such links are opened)."""
    if not is_shared(url):
        return {"error": "link not shared in any chat; use list_links for valid URLs"}
    try:
        page = fetch_page(url)
    except Exception as exc:  # noqa: BLE001 - explain the failure to the model
        detail = str(exc)
        if "ERR_CERT" in detail:
            return {"error": "site certificate is not trusted by the reader's browser", "detail": detail[:200]}
        return {"error": detail[:300], "hint": "Ensure the VPN is up."}
    final_url = page.get("final_url") or url
    if any(marker in final_url for marker in _LOGIN_MARKERS):
        return {
            "url": url,
            "final_url": final_url,
            "needs_login": True,
            "hint": f"Log in once: run `teams-reader open-link '{url}'`, sign in, close the window.",
        }
    text = page.get("text") or ""
    return {
        "url": url,
        "final_url": final_url,
        "title": page.get("title"),
        "chars": len(text),
        "truncated": len(text) > max_chars,
        "text": text[:max_chars],
    }


def consume_images(
    conn: sqlite3.Connection,
    msg_id: str,
    download: Callable[[sqlite3.Connection, str], Any],
) -> dict[str, Any]:
    """(bytes, format) of a message's images; local copies are deleted once read."""
    rows = db_mod.images_for_msg(conn, msg_id)
    if not rows:
        return {"error": f"no image for msg_id: {msg_id}"}
    for r in rows:
        if r["local_path"] and not Path(r["local_path"]).exists():
            db_mod.clear_image_path(conn, msg_id, r["url"])
    if any(not (r["local_path"] and Path(r["local_path"]).exists()) for r in rows):
        try:
            download(conn, msg_id)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "hint": "Run `teams-reader login` and ensure the VPN is up."}
    images: list[tuple[bytes, str]] = []
    rejected = 0
    for r in db_mod.images_for_msg(conn, msg_id):
        path = Path(r["local_path"]) if r["local_path"] else None
        if path is None or not path.exists():
            continue
        data = path.read_bytes()
        path.unlink()
        db_mod.clear_image_path(conn, msg_id, r["url"])
        fmt = image_format(data)
        if fmt is None:
            rejected += 1  # e.g. an HTML login page served with status 200
            continue
        images.append((data, fmt))
    if not images:
        reason = "downloaded file is not an image (session expired?)" if rejected else "download failed"
        return {"error": reason, "hint": "Run `teams-reader login` and ensure the VPN is up."}
    return {"msg_id": msg_id, "images": images}


_REPLY_GUIDANCE = (
    "Write 2-3 reply options the user can copy into this chat. Match the chat's "
    "dominant language and each participant's tone/length from `participants`. "
    "Adapt to the chat kind (group vs 1:1) and the latest messages. Offer distinct "
    "angles (e.g. agree / ask for detail / defer). Do not send anything."
)


def suggest_reply(
    conn: sqlite3.Connection,
    name_or_id: str,
    intent: str | None = None,
    limit: int = 15,
) -> dict[str, Any]:
    """Context for composing an adaptive reply: recent thread + participant
    profiles + chat kind. Claude writes the actual options from this."""
    chat_id = resolve_chat(conn, name_or_id)
    if chat_id is None:
        return {"error": f"chat not found: {name_or_id}", "recent": []}
    names = chat_names(conn)
    row = next((c for c in db_mod.list_chats(conn) if c["chat_id"] == chat_id), None)
    participants = {
        sender: profile_dict(texts)
        for sender, texts in db_mod.sender_texts(conn, chat_id).items()
    }
    return {
        "chat_id": chat_id,
        "chat_name": names.get(chat_id, chat_id),
        "kind": row["kind"] if row else None,
        "intent": intent,
        "recent": [_row_to_message(r, names) for r in db_mod.tail_chat(conn, chat_id, limit=limit)],
        "participants": participants,
        "guidance": _REPLY_GUIDANCE,
    }
