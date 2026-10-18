"""Diagnostic: find which field in the Teams cache holds the real group title.

Dumps the real work cache, then for records whose JSON contains one of the known
group names, prints the record's key names and which key holds the title (so the
resolver can target the correct field). Run:

    PYTHONPATH=. venv/bin/python tools/find_title_field.py "CBU Acceleration" "VN - internal"
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from teams_reader.cache import dump as dump_mod
from teams_reader.cache import locate as locate_mod
from teams_reader.cache.snapshot import snapshot_leveldb
from teams_reader.parse.decode import decode_stream, unwrap

TARGETS = sys.argv[1:] or ["CBU Acceleration", "VN - internal"]


def _dfindexeddb() -> str:
    return str(Path(sys.executable).with_name("dfindexeddb"))


def _key_for_value(node: dict, target: str) -> list[str]:
    """Keys whose string value contains target (direct children)."""
    return [k for k, v in node.items() if isinstance(v, str) and target in v]


def _walk(node, target, out):
    if isinstance(node, dict):
        hits = _key_for_value(node, target)
        if hits:
            out.append((hits, sorted(node.keys())))
        for v in node.values():
            _walk(v, target, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, target, out)


def main() -> None:
    leveldb = locate_mod.work_leveldb()
    if leveldb is None:
        print("no work leveldb found", file=sys.stderr)
        sys.exit(1)
    tmp = Path(tempfile.mkdtemp())
    snap = snapshot_leveldb(leveldb, tmp / leveldb.name)
    (snap.parent / snap.name.replace(".leveldb", ".blob")).mkdir(exist_ok=True)
    dump_path = dump_mod.dump_to_jsonl(snap, tmp / "d.jsonl", binary=_dfindexeddb())
    raw = dump_path.read_text(encoding="utf-8")

    for target in TARGETS:
        print(f"\n=== '{target}' ===")
        out: list = []
        for rec in decode_stream(raw):
            if isinstance(rec, dict) and "value" in rec:
                _walk(unwrap(rec["value"]), target, out)
        if not out:
            print("  not found")
        seen = set()
        for keys, record_keys in out[:5]:
            sig = (tuple(keys), tuple(record_keys))
            if sig in seen:
                continue
            seen.add(sig)
            print(f"  title key(s): {keys}")
            print(f"  record keys : {record_keys}")


if __name__ == "__main__":
    main()
