"""Extract chat messages from decoded Teams IndexedDB values.

A Teams reply-chain message is a dict carrying HTML ``content`` plus a sender
(``imdisplayname``) and/or a timestamp (``originalarrivaltime``). Messages can
be nested inside arrays and other objects, so the extractor walks recursively.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from teams_reader.parse.images import extract_image_urls
from teams_reader.parse.links import extract_link_urls

_TIME_KEYS = ("originalarrivaltime", "composetime", "originalArrivalTime", "composeTime")
_SENDER_KEYS = ("imdisplayname", "imDisplayName", "from", "creator")
_CONTENT_KEYS = ("content", "renderContent", "body")
_ID_KEYS = ("id", "clientmessageid", "clientMessageId", "messageId", "sequenceId")
_CHAT_KEYS = ("conversationId", "conversationid", "conversationLink", "threadId", "to")
_CONTROL_SENDER_PREFIXES = ("19:", "8:", "28:", "48:", "http://", "https://", "worker/")
_SYSTEM_TYPE_PREFIXES = ("ThreadActivity/", "Event/", "Control/")

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCKQUOTE_RE = re.compile(r"<blockquote\b[^>]*>(.*?)</blockquote>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class Message:
    msg_id: str
    chat_id: str | None
    sender: str
    ts: int | None
    text: str
    quoted_text: str | None
    image_urls: tuple[str, ...] = ()
    link_urls: tuple[str, ...] = ()
    version: int | None = None
    edited: bool = False
    deleted: bool = False


def _strip_html(raw: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", raw))).strip()


def _split_quote(content: str) -> tuple[str, str | None]:
    """Separate a reply's quoted parent (``<blockquote>``) from its new text."""
    quotes = _BLOCKQUOTE_RE.findall(content)
    if not quotes:
        return _strip_html(content), None
    remainder = _BLOCKQUOTE_RE.sub(" ", content)
    quoted = _strip_html(" ".join(quotes))
    return _strip_html(remainder), (quoted or None)


def _first(node: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = node.get(key)
        if value:
            return value
    return None


def _normalize_chat_id(value: Any) -> str:
    """Reduce a conversation link to the bare thread id.

    ``conversationLink`` looks like ``conversation/19:...@thread.v2;messageid=…``;
    strip the prefix and the ``;messageid`` suffix so every message of a chat
    maps to the same id (and no ``conversation/…;messageid`` junk chats appear).
    """
    text = str(value)
    if text.startswith("conversation/"):
        text = text[len("conversation/"):]
    return text.split(";", 1)[0]


def _to_ms(value: Any) -> int | None:
    try:
        number = float(value)
        if number > 1_000_000_000_000:  # epoch milliseconds
            return int(number)
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):  # ISO 8601 (chatsvc timestamps)
        try:
            return int(
                datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            return None
    return None


def _is_message(node: dict[str, Any]) -> bool:
    has_content = any(k in node for k in _CONTENT_KEYS)
    has_identity = any(k in node for k in _SENDER_KEYS) or any(k in node for k in _TIME_KEYS)
    return has_content and has_identity


def _make_id(node: dict[str, Any], chat_id: str | None, ts: int | None, text: str) -> str:
    explicit = _first(node, _ID_KEYS)
    if explicit:
        return str(explicit)
    seed = f"{chat_id}|{ts}|{text[:80]}".encode("utf-8")
    return "h:" + hashlib.sha1(seed).hexdigest()[:20]


def _version(node: dict[str, Any]) -> int | None:
    try:
        return int(node.get("version"))
    except (TypeError, ValueError):
        return None


def _build(node: dict[str, Any]) -> Message | None:
    if str(node.get("messagetype") or "").startswith(_SYSTEM_TYPE_PREFIXES):
        return None
    props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
    deleted = bool(props.get("deletetime"))
    content = _first(node, _CONTENT_KEYS)
    if deleted:
        content = ""  # tombstone: keeps a stale cached copy from resurfacing
    if not isinstance(content, str):
        return None
    sender = _first(node, _SENDER_KEYS)
    if not isinstance(sender, str) or sender.startswith(_CONTROL_SENDER_PREFIXES):
        return None
    text, quoted = _split_quote(content)
    image_urls = tuple(extract_image_urls(content))
    if not deleted and not text and not quoted and not image_urls:
        return None
    chat_id = _first(node, _CHAT_KEYS)
    chat_id = _normalize_chat_id(chat_id) if chat_id else None
    ts = _to_ms(_first(node, _TIME_KEYS))
    return Message(
        msg_id=_make_id(node, chat_id, ts, text),
        chat_id=chat_id,
        sender=sender,
        ts=ts,
        text=text,
        quoted_text=quoted,
        image_urls=image_urls,
        link_urls=tuple(extract_link_urls(content)),
        version=_version(node),
        edited=bool(props.get("edittime")),
        deleted=deleted,
    )


def _walk(node: Any) -> Iterator[Message]:
    if isinstance(node, dict):
        if _is_message(node):
            message = _build(node)
            if message is not None:
                yield message
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def extract_messages(values: Iterable[Any]) -> list[Message]:
    """Return de-duplicated messages found anywhere in the given values."""
    seen: set[str] = set()
    out: list[Message] = []
    for value in values:
        for message in _walk(value):
            if message.msg_id in seen:
                continue
            seen.add(message.msg_id)
            out.append(message)
    return out
