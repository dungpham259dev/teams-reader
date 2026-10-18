"""Debug: why does topic mining miss? Dump real cache, inspect the CBU record."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from teams_reader.cache import dump as dump_mod
from teams_reader.cache import locate as locate_mod
from teams_reader.cache.snapshot import snapshot_leveldb
from teams_reader.parse.decode import decode_stream, unwrap
from teams_reader.parse import conversations as C

TARGET = sys.argv[1] if len(sys.argv) > 1 else "CBU Acceleration Team Handover"


def main() -> None:
    lv = locate_mod.work_leveldb()
    tmp = Path(tempfile.mkdtemp())
    snap = snapshot_leveldb(lv, tmp / lv.name)
    (snap.parent / snap.name.replace(".leveldb", ".blob")).mkdir(exist_ok=True)
    dp = dump_mod.dump_to_jsonl(snap, tmp / "d.jsonl", binary=str(Path(sys.executable).with_name("dfindexeddb")))
    raw = dp.read_text(encoding="utf-8")

    for rec in decode_stream(raw):
        if not isinstance(rec, dict) or "value" not in rec:
            continue
        v = unwrap(rec["value"])
        if isinstance(v, str) and TARGET in v:
            print("MATCH as str; len", len(v))
            idm = C._THREAD_ID_RE.search(v)
            print("ascii thread id:", idm.group(0) if idm else None)
            print("topic value:", repr(C._value_after(v, C._TOPIC_RE)))
            print("shortTitle value:", repr(C._value_after(v, C._SHORTTITLE_RE)))
            print("value start:", repr(v[:90]))
            i = v.find(TARGET)
            print("around target:", repr(v[i - 20:i + len(TARGET) + 3]))
            return
        if isinstance(v, (dict, list)):
            import json
            if TARGET in json.dumps(v):
                print("MATCH inside dict/list (not str). top type:", type(v).__name__)
                if isinstance(v, dict):
                    print("keys:", list(v.keys())[:15])
                return
    print("TARGET not found in any decoded value")


if __name__ == "__main__":
    main()
