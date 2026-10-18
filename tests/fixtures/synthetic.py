"""Synthetic dfindexeddb-shaped dump builder for tests. No real chat data."""
from __future__ import annotations

import json
from typing import Any


def _record(value: Any, object_store_id: int = 1) -> dict[str, Any]:
    """Wrap a decoded value the way a dfindexeddb record carries it."""
    return {
        "__type__": "IndexedDBRecord",
        "object_store_id": object_store_id,
        "value": {"__type__": "IDBValueWrapper", "value": value},
    }


def message_value(
    msg_id: str,
    sender: str,
    content: str,
    ts: int,
    chat_id: str = "19:demo@thread.v2",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "imdisplayname": sender,
        "content": content,
        "originalarrivaltime": ts,
        "conversationId": chat_id,
    }


def build_dump(records: list[dict[str, Any]]) -> str:
    """Render records as JSONL, as dfindexeddb's ``-o jsonl`` does."""
    return "\n".join(json.dumps(_record(r)) for r in records)


def sample_dump() -> str:
    """A small, representative dump: a normal message, a reply with a quote,
    a control row, and a message stored as a nested JSON string."""
    normal = message_value("m1", "Alice", "<p>hello team</p>", 1_787_000_000_000)
    reply = message_value(
        "m2",
        "Bob",
        "<blockquote>hello team</blockquote><p>hi Alice, on it</p>",
        1_787_000_100_000,
    )
    control = {
        "id": "c1",
        "from": "19:system@thread.v2",
        "content": "<systemmessage>Bob joined</systemmessage>",
        "originalarrivaltime": 1_787_000_050_000,
    }
    nested_json = {
        "__type__": "IDBValueWrapper",
        "value": json.dumps(
            message_value("m3", "Carol", "<p>deploying now</p>", 1_787_000_200_000)
        ),
    }
    records = [normal, reply, control]
    dump = build_dump(records)
    # Append one record whose value is a JSON string (double-encoded).
    extra = {
        "__type__": "IndexedDBRecord",
        "object_store_id": 1,
        "value": nested_json,
    }
    return dump + "\n" + json.dumps(extra)
