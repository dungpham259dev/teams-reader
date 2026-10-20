"""Pure helpers for the Teams chatsvc messages endpoint.

A chatsvc messages response looks like::

    {"messages": [ {message dicts} ],
     "_metadata": {"backwardLink": "https://.../messages?...", ...}}

The message dicts carry the same fields the local cache stores
(``content``/``imdisplayname``/``originalarrivaltime``), so message extraction
reuses :func:`teams_reader.parse.messages.extract_messages`. Older history is
paged by following ``_metadata.backwardLink`` until it is absent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

from teams_reader.parse.messages import Message, _to_ms, extract_messages

_MESSAGES_RE = re.compile(r"/v1/users/ME/conversations/(?P<cid>[^/]+)/messages")
_BASE_RE = re.compile(r"(?P<base>https?://.+?)/v1/users/ME/conversations")
_BACKWARD_KEYS = ("backwardLink", "backwardlink", "backward_link")
_DEFAULT_QUERY = "pageSize=200&startTime=1&view=msnp24Equivalent"
_TEMPLATE_CID = "__CID__"
_CONVERSATIONS_QUERY = "view=msnp24Equivalent&pageSize=500"
_KINDS = {
    "OneToOneChat": "chat",
    "Chat": "group",
    "Meeting": "meeting",
    "StreamOfNotes": "notes",
}
_KEPT_STREAMS = {"48:notes"}


@dataclass(frozen=True)
class Conversation:
    """One entry of the server's conversation list."""

    chat_id: str
    topic: str | None
    kind: str | None
    last_activity_ms: int | None


def conversations_url(template_url: str) -> str:
    """The conversation-list URL on the same chatsvc host as ``template_url``."""
    match = _BASE_RE.search(template_url or "")
    if not match:
        raise ValueError("not a chatsvc URL")
    return f"{match.group('base')}/v1/users/ME/conversations?{_CONVERSATIONS_QUERY}"


def _last_activity(conv: dict) -> int | None:
    last = conv.get("lastMessage") or {}
    stamps = [_to_ms(last.get(k)) for k in ("originalarrivaltime", "composetime")]
    stamps.append(_to_ms(conv.get("lastUpdatedMessageId")))
    # An edit or delete leaves lastMessage alone; only these versions move.
    stamps.append(_to_ms(conv.get("lastUpdatedMessageVersion")))
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


def parse_conversations(payload: object) -> list[Conversation]:
    """Real chats (titles, kinds, last activity) from a conversation-list page.

    Activity/notification streams (``48:*`` other than notes) are skipped.
    """
    if not isinstance(payload, dict):
        return []
    out: list[Conversation] = []
    for conv in payload.get("conversations") or []:
        chat_id = conv.get("id") if isinstance(conv, dict) else None
        if not chat_id or (chat_id.startswith("48:") and chat_id not in _KEPT_STREAMS):
            continue
        props = conv.get("threadProperties") or {}
        topic = (props.get("topic") or "").strip() or None
        out.append(
            Conversation(
                chat_id=chat_id,
                topic=topic,
                kind=_KINDS.get(props.get("productThreadType")),
                last_activity_ms=_last_activity(conv),
            )
        )
    return out


def is_conversations_url(url: str) -> bool:
    return "/v1/users/ME/conversations" in (url or "")


def messages_template(conversations_url: str) -> str:
    """Build a messages URL template from any chatsvc conversations request.

    Boot always hits ``/v1/users/ME/conversations`` (listing chats) even when no
    ``/messages`` call fires; reuse its host to construct a messages template
    that :func:`with_conversation` then fills per chat.
    """
    match = _BASE_RE.search(conversations_url or "")
    if not match:
        raise ValueError("not a chatsvc conversations URL")
    base = match.group("base")
    return f"{base}/v1/users/ME/conversations/{_TEMPLATE_CID}/messages?{_DEFAULT_QUERY}"


def is_messages_url(url: str) -> bool:
    return bool(_MESSAGES_RE.search(url or ""))


def conversation_of(url: str) -> str | None:
    """The (decoded) conversation id embedded in a messages URL, if any."""
    match = _MESSAGES_RE.search(url or "")
    return unquote(match.group("cid")) if match else None


def with_conversation(template_url: str, conversation_id: str) -> str:
    """Reuse a captured messages URL (host + query) for another conversation."""
    if not _MESSAGES_RE.search(template_url or ""):
        raise ValueError("not a chatsvc messages URL")
    encoded = quote(conversation_id, safe="")
    return _MESSAGES_RE.sub(
        f"/v1/users/ME/conversations/{encoded}/messages", template_url, count=1
    )


def parse_page(payload: object, chat_id: str | None = None) -> list[Message]:
    """Extract messages from one chatsvc page.

    When ``chat_id`` is given, every message is attributed to it — the request
    already scoped the page to one conversation, so trust that over per-message
    links (which are full URLs, not thread ids).
    """
    messages = extract_messages([payload])
    if chat_id is None:
        return messages
    from dataclasses import replace

    return [replace(m, chat_id=chat_id) for m in messages]


def backward_link(payload: object) -> str | None:
    """The URL of the next (older) page, or ``None`` when history is exhausted."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("_metadata")
    if not isinstance(meta, dict):
        return None
    for key in _BACKWARD_KEYS:
        value = meta.get(key)
        if value:
            return str(value)
    return None
