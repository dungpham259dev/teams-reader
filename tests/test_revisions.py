import sqlite3

import pytest

from teams_reader.mcp import tools
from teams_reader.parse.messages import Message, extract_messages
from teams_reader.store import db as db_mod


def _m(mid, text, version=None, ts=100, chat="19:a", edited=False, deleted=False, sender="Ann"):
    return Message(mid, chat, sender, ts, text, None, version=version, edited=edited, deleted=deleted)


def _node(**over):
    base = {
        "id": "m1", "conversationId": "19:a", "imdisplayname": "Ann",
        "originalarrivaltime": "2026-10-16T06:41:36Z", "version": "1792132321707",
        "content": "<p>fixed text</p>", "properties": {"edittime": 1792132321707},
    }
    base.update(over)
    return base


@pytest.mark.unit
def test_parser_reads_version_and_edit_flag():
    (msg,) = extract_messages([_node()])
    assert msg.version == 1792132321707 and msg.edited is True and msg.deleted is False


@pytest.mark.unit
def test_parser_keeps_deleted_messages_as_tombstones():
    (msg,) = extract_messages([_node(content="", properties={"deletetime": 1792132400000})])
    assert msg.deleted is True and msg.text == ""


@pytest.mark.unit
def test_newer_version_replaces_text_and_reindexes_search():
    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(conn, [_m("m1", "typo txet", version=1)])
    db_mod.upsert_messages(conn, [_m("m1", "fixed text", version=2, edited=True)])
    db_mod.upsert_messages(conn, [_m("m1", "typo txet", version=1)])  # stale cache copy
    row = conn.execute("SELECT text, edited, version FROM messages").fetchone()
    assert tuple(row) == ("fixed text", 1, 2)
    assert [m["msg_id"] for m in tools.search(conn, "fixed")] == ["m1"]
    assert tools.search(conn, "txet") == []


@pytest.mark.unit
def test_deleted_messages_are_hidden_everywhere():
    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(conn, [_m("m1", "secret plan", version=1), _m("m2", "kept", version=1, ts=200)])
    db_mod.upsert_messages(conn, [_m("m1", "", version=5, deleted=True)])
    db_mod.upsert_messages(conn, [_m("m1", "secret plan", version=1)])  # cache still has it
    assert tools.search(conn, "secret") == []
    assert [m["msg_id"] for m in tools.recent(conn)] == ["m2"]
    assert [m["msg_id"] for m in tools.read_chat(conn, "19:a")["messages"]] == ["m2"]
    assert tools.list_chats(conn)[0]["message_count"] == 1


@pytest.mark.unit
def test_read_chat_marks_edited_messages():
    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(conn, [_m("m1", "v2", version=2, edited=True)])
    assert tools.read_chat(conn, "19:a")["messages"][0]["edited"] is True


@pytest.mark.unit
def test_connect_migrates_an_old_store(tmp_path):
    path = tmp_path / "old.db"
    seed = db_mod.connect(path)
    db_mod.upsert_messages(seed, [_m("m1", "hi")])
    seed.close()
    old = sqlite3.connect(path)  # strip the new columns: a store from before this change
    old.execute("DROP TRIGGER messages_au")
    for column in ("version", "edited", "deleted"):
        old.execute(f"ALTER TABLE messages DROP COLUMN {column}")
    old.commit()
    old.close()
    conn = db_mod.connect(path)
    db_mod.upsert_messages(conn, [_m("m1", "hi edited", version=3, edited=True)])
    assert conn.execute("SELECT text FROM messages").fetchone()[0] == "hi edited"
    assert [m["msg_id"] for m in tools.search(conn, "edited")] == ["m1"]


@pytest.mark.unit
def test_search_filters_by_chat_and_time():
    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(conn, [
        _m("a", "deploy keycloak", ts=100, chat="19:a"),
        _m("b", "deploy sigma", ts=200, chat="19:b"),
        _m("c", "deploy again", ts=300, chat="19:a"),
    ])
    db_mod.set_chat_name(conn, "19:b", "Sigma Team")
    ids = lambda out: sorted(m["msg_id"] for m in out)
    assert ids(tools.search(conn, "deploy")) == ["a", "b", "c"]
    assert ids(tools.search(conn, "deploy", chat="Sigma")) == ["b"]
    assert ids(tools.search(conn, "deploy", since=150)) == ["b", "c"]
    assert ids(tools.search(conn, "deploy", until=250)) == ["a", "b"]
    assert tools.search(conn, "deploy", chat="nope") == []
