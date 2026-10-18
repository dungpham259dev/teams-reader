"""Opt-in end-to-end test against a REAL Teams LevelDB and the REAL dfindexeddb
binary (snapshot -> dump -> parse -> store).

Skipped by default. To run it against your own cache (no data is committed):

    TEAMS_READER_REAL_LEVELDB="$HOME/Library/Containers/com.microsoft.teams2/\
Data/Library/Application Support/Microsoft/MSTeams/EBWebView/WV2Profile_tfw/\
IndexedDB/https_teams.microsoft.com_0.indexeddb.leveldb" \
      PYTHONPATH=. venv/bin/pytest -q tests/test_real_pipeline.py

It asserts the real cache yields at least one message, i.e. the whole pipeline
works end-to-end on genuine dfindexeddb output.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from teams_reader.cache import dump as dump_mod
from teams_reader.cache.snapshot import snapshot_leveldb
from teams_reader.ingest import ingest_from_dump
from teams_reader.store import db as db_mod

_REAL = os.environ.get("TEAMS_READER_REAL_LEVELDB")


def _dfindexeddb_bin() -> str:
    return str(Path(sys.executable).with_name("dfindexeddb"))


@pytest.mark.integration
@pytest.mark.skipif(not _REAL, reason="set TEAMS_READER_REAL_LEVELDB to run")
def test_full_pipeline_on_real_leveldb(tmp_path):
    source = Path(_REAL)
    assert source.is_dir(), f"not a leveldb dir: {source}"
    if not Path(_dfindexeddb_bin()).exists():
        pytest.skip("dfindexeddb not installed")

    snap = snapshot_leveldb(source, tmp_path / source.name)
    (snap.parent / snap.name.replace(".leveldb", ".blob")).mkdir(exist_ok=True)
    dump_path = dump_mod.dump_to_jsonl(snap, tmp_path / "dump.jsonl", binary=_dfindexeddb_bin())

    conn = db_mod.connect(":memory:")
    try:
        result = ingest_from_dump(conn, dump_path)
        assert result.total_messages > 0, "real cache yielded no messages"
        chats = db_mod.list_chats(conn)
        assert chats, "no chats recorded"
        # Name resolution (chatTitle) must work on the real schema, not just
        # fall back to thread ids for every chat.
        assert result.chats_named > 0, "no chat display names resolved"
    finally:
        conn.close()
