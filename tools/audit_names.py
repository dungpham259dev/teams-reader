"""Audit: for every chat id the messages use, is a name available anywhere in
the dump (topic / shortTitle / sourceThreadTopic), and does it link?"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from teams_reader import config
from teams_reader.cache import dump as dump_mod
from teams_reader.cache import locate as locate_mod
from teams_reader.cache.snapshot import snapshot_leveldb
from teams_reader.parse.decode import decode_stream, decoded_values, unwrap
from teams_reader.parse import conversations as C
from teams_reader.parse.messages import extract_messages
from teams_reader.store import db as db_mod


def main() -> None:
    conn = db_mod.connect(config.db_path())
    unnamed = {r["chat_id"] for r in db_mod.list_chats(conn)
               if not r["display_name"] and r["message_count"]}
    print("unnamed chat ids with messages:", len(unnamed))

    lv = locate_mod.work_leveldb()
    tmp = Path(tempfile.mkdtemp())
    snap = snapshot_leveldb(lv, tmp / lv.name)
    (snap.parent / snap.name.replace(".leveldb", ".blob")).mkdir(exist_ok=True)
    dp = dump_mod.dump_to_jsonl(snap, tmp / "d.jsonl", binary=str(Path(sys.executable).with_name("dfindexeddb")))
    raw = dp.read_text(encoding="utf-8")

    for cid in unnamed:
        found = []
        short = cid.split(":", 1)[1].split("@", 1)[0][:16]  # distinctive middle
        for rec in decode_stream(raw):
            if not isinstance(rec, dict) or "value" not in rec:
                continue
            v = unwrap(rec["value"])
            if not isinstance(v, str) or short not in v:
                continue
            for label, rgx in (("topic", C._TOPIC_RE), ("shortTitle", C._SHORTTITLE_RE),
                               ("sourceThreadTopic", C._SRC_TOPIC_RE)):
                val = C._value_after(v, rgx)
                if val:
                    found.append(f"{label}={val!r}")
        print(f"\n{cid}")
        print("  names seen in co-located records:", found[:6] or "NONE")


if __name__ == "__main__":
    main()
