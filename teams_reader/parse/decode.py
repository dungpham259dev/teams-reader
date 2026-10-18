"""Decode a dfindexeddb JSONL dump into plain Python values.

A dfindexeddb dump is a stream of records (one JSON object per line, or several
concatenated). Each record's decoded IndexedDB value is wrapped in
structured-clone envelopes of the shape ``{"__type__": ..., "value": X, ...}``;
``unwrap`` collapses those to the plain value and parses any nested JSON string.
"""
from __future__ import annotations

import json
from typing import Any, Iterator


def decode_stream(raw: str) -> Iterator[Any]:
    """Yield each top-level JSON value from a dump (JSONL or concatenated)."""
    decoder = json.JSONDecoder()
    i, n = 0, len(raw)
    while i < n:
        while i < n and raw[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        try:
            obj, end = decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            newline = raw.find("\n", i)
            if newline == -1:
                break
            i = newline + 1
            continue
        yield obj
        i = end


def unwrap(node: Any) -> Any:
    """Collapse structured-clone wrappers and parse nested JSON strings."""
    if isinstance(node, dict):
        if "__type__" in node and "value" in node and len(node) <= 5:
            return unwrap(node["value"])
        return {k: unwrap(v) for k, v in node.items() if k != "__type__"}
    if isinstance(node, list):
        return [unwrap(v) for v in node]
    if isinstance(node, str):
        stripped = node.strip()
        if stripped[:1] in "{[" and stripped[-1:] in "}]":
            try:
                return unwrap(json.loads(stripped))
            except json.JSONDecodeError:
                return node
    return node


def decoded_values(raw: str) -> Iterator[Any]:
    """Yield the unwrapped ``value`` of each record that carries one."""
    for record in decode_stream(raw):
        if isinstance(record, dict) and "value" in record:
            yield unwrap(record["value"])
