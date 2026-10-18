"""For unnamed chats, find their thread blob in the dump and show what name
fields are present, to understand why resolution missed them."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from teams_reader import config
from teams_reader.cache import dump as dump_mod
from teams_reader.cache import locate as locate_mod
from teams_reader.cache.snapshot import snapshot_leveldb
from teams_reader.parse.decode import decode_stream, unwrap
from teams_reader.parse import conversations as C
from teams_reader.store import db as db_mod


def main() -> None:
    conn = db_mod.connect(config.db_path())
    unnamed = [r["chat_id"] for r in db_mod.list_chats(conn)
               if not r["display_name"] and r["message_count"]]
    top = sorted(unnamed, key=lambda cid: -db_mod.read_chat(conn, cid, limit=10000).__len__())[:3]
    print("top unnamed chat_ids:", top)

    lv = locate_mod.work_leveldb()
    tmp = Path(tempfile.mkdtemp())
    snap = snapshot_leveldb(lv, tmp / lv.name)
    (snap.parent / snap.name.replace(".leveldb", ".blob")).mkdir(exist_ok=True)
    dp = dump_mod.dump_to_jsonl(snap, tmp / "d.jsonl", binary=str(Path(sys.executable).with_name("dfindexeddb")))
    raw = dp.read_text(encoding="utf-8")

    for cid in top:
        print(f"\n=== {cid} ===")
        best = None
        for rec in decode_stream(raw):
            if not isinstance(rec, dict) or "value" not in rec:
                continue
            v = unwrap(rec["value"])
            if isinstance(v, str) and cid in v and ('topic"' in v or 'shortTitle"' in v):
                # prefer the record that is the thread (has threadType)
                if "threadType" in v and (best is None or len(v) > len(best)):
                    best = v
        if best is None:
            print("  no thread blob with topic/shortTitle found for this id")
            continue
        print("  blob len:", len(best))
        print("  topic value:", repr(C._value_after(best, C._TOPIC_RE)))
        print("  shortTitle value:", repr(C._value_after(best, C._SHORTTITLE_RE)))
        print("  first thread id in blob:", C._THREAD_ID_RE.search(best).group(0))


if __name__ == "__main__":
    main()
