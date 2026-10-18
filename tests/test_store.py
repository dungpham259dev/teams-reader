import pytest

from teams_reader.parse.messages import Message
from teams_reader.store import db


def _msg(msg_id, chat="19:a@thread.v2", sender="Alice", ts=1000, text="hello", quoted=None):
    return Message(msg_id=msg_id, chat_id=chat, sender=sender, ts=ts, text=text, quoted_text=quoted)


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


@pytest.mark.unit
def test_upsert_counts_new_and_ignores_duplicates(conn):
    added = db.upsert_messages(conn, [_msg("m1"), _msg("m2")])
    assert added == 2
    again = db.upsert_messages(conn, [_msg("m1"), _msg("m3")])
    assert again == 1


@pytest.mark.unit
def test_search_finds_by_text(conn):
    db.upsert_messages(conn, [_msg("m1", text="deploying keycloak now"), _msg("m2", text="lunch?")])
    rows = db.search(conn, "keycloak")
    assert [r["msg_id"] for r in rows] == ["m1"]


@pytest.mark.unit
def test_read_chat_filters_since_and_orders(conn):
    db.upsert_messages(
        conn,
        [
            _msg("m1", ts=100, text="first"),
            _msg("m2", ts=200, text="second"),
            _msg("m3", ts=300, text="third"),
        ],
    )
    rows = db.read_chat(conn, "19:a@thread.v2", since_ts=150)
    assert [r["msg_id"] for r in rows] == ["m2", "m3"]


@pytest.mark.unit
def test_recent_returns_newest_first(conn):
    db.upsert_messages(conn, [_msg("m1", ts=100), _msg("m2", ts=300), _msg("m3", ts=200)])
    rows = db.recent(conn, since_ts=0)
    assert [r["msg_id"] for r in rows] == ["m2", "m3", "m1"]


@pytest.mark.unit
def test_refresh_chats_sets_last_ts(conn):
    db.upsert_messages(conn, [_msg("m1", ts=100), _msg("m2", ts=500)])
    chat = db.list_chats(conn)[0]
    assert chat["last_msg_ts"] == 500
    assert chat["message_count"] == 2


@pytest.mark.unit
def test_set_chat_name(conn):
    db.upsert_messages(conn, [_msg("m1")])
    db.set_chat_name(conn, "19:a@thread.v2", "Project Sigma", kind="group")
    chat = db.list_chats(conn)[0]
    assert chat["display_name"] == "Project Sigma"
    assert chat["kind"] == "group"


@pytest.mark.unit
def test_high_water_and_state(conn):
    db.upsert_messages(conn, [_msg("m1", ts=100), _msg("m2", ts=900)])
    assert db.high_water_ts(conn) == 900
    db.set_state(conn, "last_sync", "900")
    assert db.get_state(conn, "last_sync") == "900"


@pytest.mark.unit
def test_search_excludes_deleted_from_fts(conn):
    db.upsert_messages(conn, [_msg("m1", text="unique token zzz")])
    conn.execute("DELETE FROM messages WHERE msg_id = 'm1'")
    conn.commit()
    assert db.search(conn, "zzz") == []


@pytest.mark.unit
def test_upsert_backfills_missing_ts_without_overwriting(conn):
    db.upsert_messages(conn, [_msg("m1", ts=None), _msg("m2", ts=500)])
    added = db.upsert_messages(conn, [_msg("m1", ts=900), _msg("m2", ts=700)])
    rows = {r["msg_id"]: r["ts"] for r in conn.execute("SELECT msg_id, ts FROM messages")}
    assert added == 0
    assert rows == {"m1": 900, "m2": 500}
    assert db.list_chats(conn)[0]["last_msg_ts"] == 900


@pytest.mark.unit
def test_purge_non_messages_drops_junk_rows(conn):
    db.upsert_messages(conn, [
        _msg("ok"),
        _msg("j1", sender="https://h/v1/users/ME/contacts/19:x"),
        _msg("j2", sender="worker/8:orgid:abc", text="meeting Play"),
    ])
    removed = db.purge_non_messages(conn)
    assert removed == 2
    assert [r["msg_id"] for r in conn.execute("SELECT msg_id FROM messages")] == ["ok"]
    assert db.search(conn, '"Play"') == []


@pytest.mark.unit
def test_tail_chat_treats_unknown_ts_as_oldest(conn):
    db.upsert_messages(conn, [_msg("a", ts=None), _msg("b", ts=100), _msg("c", ts=200)])
    assert [r["msg_id"] for r in db.tail_chat(conn, "19:a@thread.v2", limit=2)] == ["b", "c"]
    assert [r["msg_id"] for r in db.tail_chat(conn, "19:a@thread.v2", limit=10)] == ["a", "b", "c"]


@pytest.mark.unit
def test_chat_senders_counts_per_chat(conn):
    db.upsert_messages(conn, [
        _msg("a", sender="Bob"), _msg("b", sender="Bob"), _msg("c", sender="Carol"),
        _msg("d", chat="48:notes", sender="Me"),
    ])
    assert db.chat_senders(conn) == {
        "19:a@thread.v2": [("Bob", 2), ("Carol", 1)],
        "48:notes": [("Me", 1)],
    }


@pytest.mark.unit
def test_purge_keeps_real_names_that_look_like_prefixes(conn):
    db.upsert_messages(conn, [_msg("a", sender="Http Team"), _msg("b", sender="HTTPS Bot"), _msg("c", sender="Workers Union")])
    assert db.purge_non_messages(conn) == 0


@pytest.mark.unit
def test_purge_drops_empty_system_streams_but_keeps_notes(conn):
    db.set_chat_name(conn, "48:mentions", "x")
    db.set_chat_name(conn, "48:notes", "y")
    db.set_chat_name(conn, "19:meeting_a@thread.v2", "Empty meeting")
    db.purge_non_messages(conn)
    assert {r["chat_id"] for r in db.list_chats(conn)} == {"48:notes", "19:meeting_a@thread.v2"}
