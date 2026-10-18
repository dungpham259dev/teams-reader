import pytest

from teams_reader.parse.messages import Message
from teams_reader.store import db as db_mod


@pytest.mark.unit
def test_clear_all_empties_tables_and_fts():
    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(
        conn,
        [Message("m1", "19:a@thread.v2", "Alice", 1, "secret token", None)],
    )
    db_mod.set_chat_name(conn, "19:a@thread.v2", "Chat", "group")
    db_mod.set_state(conn, "k", "v")

    db_mod.clear_all(conn)

    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0] == 0
    assert db_mod.get_state(conn, "k") is None
    assert db_mod.search(conn, "secret") == []
    conn.close()
