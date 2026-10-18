"""Resolve chat ids to human display names from decoded Teams values.

Teams stores each thread's metadata in its own object store. dfindexeddb often
leaves that record's value as a raw serialized string (not a parsed dict), with
the thread id and title embedded as ``"<len>key"<len>value"`` runs. So two
strategies are used, with priority so the real group title always wins:

- a raw thread blob is regex-mined for the thread id plus its ``topic`` (a named
  group, highest priority) or ``shortTitle`` (an auto-named group / 1:1);
- a dict record is read for the reliable ``chatTitle`` / ``topic`` fields only
  (generic ``name``/``title`` are skipped — they also hold mentions and roles
  like "Everyone", which must not become chat names).

Unresolved ids fall back to the raw id at display time.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_DICT_NAME_KEYS = ("chatTitle", "topic", "threadTopic")

_THREAD_ID_RE = re.compile(r"19:[0-9A-Za-z._\-]+@(?:thread\.v2|thread\.skype|unq\.gbl\.spaces)")

# dfindexeddb renders the blob's control bytes as LITERAL escape text, so a field
# `"<keylen>key"<valuelen><value>"` appears as e.g. `\x05topic"\x1ECBU ...Team"`,
# where `\x05`, `\x1E` are four literal characters each. We locate a key by its
# own length-escape, then drop the value's leading length-escape and un-escape
# the rest (accented bytes like `\xE7` -> `ç`).
_ESCAPE = r"\\x[0-9A-Fa-f]{2}"
_UNESCAPE_RE = re.compile(_ESCAPE)


def _marker_re(keyname: str) -> re.Pattern[str]:
    return re.compile(_ESCAPE + re.escape(keyname) + '"')


_TOPIC_RE = _marker_re("topic")
_SHORTTITLE_RE = _marker_re("shortTitle")
# Activity-feed records pair the real chat id with its title.
_SRC_TOPIC_RE = _marker_re("sourceThreadTopic")
_SRC_ID_RE = _marker_re("sourceThreadId")


def _unescape(text: str) -> str:
    return _UNESCAPE_RE.sub(lambda m: chr(int(m.group(0)[2:], 16)), text)


def _strip_len_prefix(seg: str) -> str:
    """Drop the LEB128 value-length varint. Each byte is either a `\\xHH` escape
    (control / high-bit bytes) or a literal character (0x20-0x7E); continuation
    bytes have the high bit set, the final length byte does not."""
    i = 0
    while i < len(seg):
        if seg[i : i + 2] == "\\x" and _UNESCAPE_RE.match(seg, i):
            byte = int(seg[i + 2 : i + 4], 16)
            i += 4
        else:
            byte = ord(seg[i])
            i += 1
        if byte < 0x80:
            break
    return seg[i:]


def _value_after(text: str, marker_re: re.Pattern[str]) -> str | None:
    """Value of a serialized field: after the key, drop the length prefix and
    un-escape the remaining text up to the next quote."""
    match = marker_re.search(text)
    if not match:
        return None
    start = match.end()
    end = text.find('"', start)
    if end == -1:
        return None
    return _unescape(_strip_len_prefix(text[start:end])).strip() or None

# Higher wins.
_PRI_TOPIC = 3
_PRI_SHORTTITLE = 2
_PRI_DICT = 2

Resolved = dict[str, tuple[str, str, int]]  # chat_id -> (name, kind, priority)


def _looks_like_chat_id(value: Any) -> bool:
    return isinstance(value, str) and (
        "@thread" in value
        or "@unq.gbl.spaces" in value
        or value.startswith(("19:", "48:", "8:"))
    )


def _kind(chat_id: str) -> str:
    return "group" if "@thread.v2" in chat_id or "@thread.skype" in chat_id else "chat"


def _add(out: Resolved, known: set[str] | None, chat_id: str, name: str, priority: int) -> None:
    name = name.strip()
    if not name or (known is not None and chat_id not in known):
        return
    existing = out.get(chat_id)
    if existing is not None and existing[2] >= priority:
        return
    out[chat_id] = (name, _kind(chat_id), priority)


def _dict_name(node: dict[str, Any]) -> str | None:
    for key in _DICT_NAME_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _mine_thread_blob(text: str, known: set[str] | None, out: Resolved) -> None:
    id_match = _THREAD_ID_RE.search(text)
    if not id_match:
        return
    chat_id = id_match.group(0)
    topic = _value_after(text, _TOPIC_RE)
    if topic:
        _add(out, known, chat_id, topic, _PRI_TOPIC)
    short = _value_after(text, _SHORTTITLE_RE)
    if short:
        _add(out, known, chat_id, short, _PRI_SHORTTITLE)

    # Activity-feed records name a chat via sourceThreadId + sourceThreadTopic;
    # the record's own first id is a notifications thread, so use sourceThreadId.
    src_topic = _value_after(text, _SRC_TOPIC_RE)
    if src_topic:
        src_id_raw = _value_after(text, _SRC_ID_RE)
        src_id = _THREAD_ID_RE.search(src_id_raw) if src_id_raw else None
        if src_id:
            _add(out, known, src_id.group(0), src_topic, _PRI_TOPIC)


def _walk(node: Any, known: set[str] | None, out: Resolved) -> None:
    if isinstance(node, dict):
        name = _dict_name(node)
        if name:
            for value in node.values():
                if _looks_like_chat_id(value):
                    _add(out, known, value, name, _PRI_DICT)
        for value in node.values():
            _walk(value, known, out)
    elif isinstance(node, list):
        for value in node:
            _walk(value, known, out)
    elif isinstance(node, str) and len(node) > 40:
        _mine_thread_blob(node, known, out)


def resolve_names(
    values: Iterable[Any], known_ids: set[str] | None = None
) -> dict[str, tuple[str, str]]:
    """Map ``chat_id -> (display_name, kind)`` from decoded values.

    When ``known_ids`` is given, only chats whose id appears among the messages'
    conversation ids are named, so roster/member records never leak in.
    """
    resolved: Resolved = {}
    for value in values:
        _walk(value, known_ids, resolved)
    return {cid: (name, kind) for cid, (name, kind, _pri) in resolved.items()}
