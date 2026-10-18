import pytest

from teams_reader.mcp import tools
from teams_reader.parse.messages import Message
from teams_reader.store import db as db_mod


def _msg(mid, chat="19:a@thread.v2", sender="Alice", ts=1000, text="hello"):
    return Message(msg_id=mid, chat_id=chat, sender=sender, ts=ts, text=text, quoted_text=None)


@pytest.fixture()
def conn():
    c = db_mod.connect(":memory:")
    db_mod.upsert_messages(
        c,
        [
            _msg("m1", text="deploying keycloak to prod", ts=100),
            _msg("m2", text="lunch at noon?", ts=200),
            _msg("m3", chat="19:b@thread.v2", text="other chat", ts=300),
        ],
    )
    db_mod.set_chat_name(c, "19:a@thread.v2", "Project Sigma", "group")
    db_mod.set_chat_name(c, "19:b@thread.v2", "Random", "group")
    yield c
    c.close()


@pytest.mark.unit
def test_list_chats_uses_names(conn):
    names = {c["display_name"] for c in tools.list_chats(conn)}
    assert names == {"Project Sigma", "Random"}


@pytest.mark.unit
def test_resolve_chat_by_id_and_partial_name(conn):
    assert tools.resolve_chat(conn, "19:a@thread.v2") == "19:a@thread.v2"
    assert tools.resolve_chat(conn, "Sigma") == "19:a@thread.v2"
    assert tools.resolve_chat(conn, "nonexistent") is None


@pytest.mark.unit
def test_read_chat_by_name(conn):
    out = tools.read_chat(conn, "Sigma")
    assert out["chat_id"] == "19:a@thread.v2"
    assert {m["msg_id"] for m in out["messages"]} == {"m1", "m2"}


@pytest.mark.unit
def test_read_chat_not_found(conn):
    out = tools.read_chat(conn, "ghost")
    assert out["messages"] == []
    assert "error" in out


@pytest.mark.unit
def test_search_basic(conn):
    rows = tools.search(conn, "keycloak")
    assert [r["msg_id"] for r in rows] == ["m1"]


@pytest.mark.unit
def test_search_sanitizes_special_characters(conn):
    # Raw FTS syntax chars must not raise; they are tokenised safely.
    rows = tools.search(conn, 'keycloak"() OR *')
    assert [r["msg_id"] for r in rows] == ["m1"]


@pytest.mark.unit
def test_search_empty_query_returns_nothing(conn):
    assert tools.search(conn, "   ") == []
    assert tools.search(conn, "") == []


@pytest.mark.unit
def test_search_unicode_query(conn):
    db_mod.upsert_messages(conn, [_msg("m9", text="triển khai xong rồi", ts=400)])
    rows = tools.search(conn, "triển")
    assert [r["msg_id"] for r in rows] == ["m9"]


@pytest.mark.unit
def test_recent_orders_desc(conn):
    rows = tools.recent(conn, since=0)
    assert [r["msg_id"] for r in rows][:1] == ["m3"]


@pytest.mark.unit
def test_ask_returns_context(conn):
    out = tools.ask(conn, "what about keycloak deploy")
    assert out["question"].startswith("what about")
    assert [m["msg_id"] for m in out["context"]] == ["m1"]


@pytest.mark.unit
def test_chat_profile(conn):
    out = tools.chat_profile(conn, "Sigma")
    assert out["chat_id"] == "19:a@thread.v2"
    assert "Alice" in out["participants"]
    assert out["participants"]["Alice"]["language"] == "en"


@pytest.mark.unit
def test_chat_profile_not_found(conn):
    assert "error" in tools.chat_profile(conn, "ghost")


@pytest.mark.unit
def test_suggest_reply_returns_context(conn):
    out = tools.suggest_reply(conn, "Sigma", intent="agree")
    assert out["chat_id"] == "19:a@thread.v2"
    assert out["chat_name"] == "Project Sigma"
    assert out["intent"] == "agree"
    assert [m["msg_id"] for m in out["recent"]] == ["m1", "m2"]  # chronological
    assert "Alice" in out["participants"]
    assert "guidance" in out


@pytest.mark.unit
def test_suggest_reply_not_found(conn):
    out = tools.suggest_reply(conn, "ghost")
    assert "error" in out
    assert out["recent"] == []


@pytest.mark.unit
def test_list_images_returns_attachments(conn):
    with_img = Message(
        msg_id="img1", chat_id="19:a@thread.v2", sender="Bob", ts=400,
        text="see this", quoted_text=None,
        image_urls=("https://h/v1/objects/OBJ/views/imgo",),
    )
    db_mod.upsert_messages(conn, [with_img])
    out = tools.list_images(conn, "Sigma")
    assert out["chat_id"] == "19:a@thread.v2"
    assert [i["url"] for i in out["images"]] == ["https://h/v1/objects/OBJ/views/imgo"]
    assert "local_path" not in out["images"][0]


@pytest.mark.unit
def test_list_images_unknown_chat(conn):
    out = tools.list_images(conn, "nope-xyz")
    assert "error" in out and out["images"] == []


def _image_conn(tmp_path, n=1):
    c = db_mod.connect(":memory:")
    urls = tuple(f"https://h/v1/objects/O{i}/views/imgo" for i in range(n))
    db_mod.upsert_messages(
        c, [Message("mi", "19:a@thread.v2", "Alice", 1, "pic", None, image_urls=urls)]
    )
    return c, urls


@pytest.mark.unit
def test_consume_images_returns_bytes_and_deletes_files(tmp_path):
    c, urls = _image_conn(tmp_path, n=2)

    def download(conn, msg_id):
        for i, url in enumerate(urls):
            f = tmp_path / f"{i}.jpg"
            f.write_bytes(b"\xff\xd8img%d" % i)
            db_mod.set_image_path(conn, msg_id, url, str(f))

    out = tools.consume_images(c, "mi", download)

    assert [d for d, _ in out["images"]] == [b"\xff\xd8img0", b"\xff\xd8img1"]
    assert list(tmp_path.glob("*.jpg")) == []
    assert all(r["local_path"] is None for r in db_mod.images_for_msg(c, "mi"))


@pytest.mark.unit
def test_consume_images_uses_existing_file_without_download(tmp_path):
    c, urls = _image_conn(tmp_path)
    f = tmp_path / "x.jpg"
    f.write_bytes(b"\xff\xd8cached")
    db_mod.set_image_path(c, "mi", urls[0], str(f))

    def download(conn, msg_id):
        raise AssertionError("must not download")

    assert tools.consume_images(c, "mi", download)["images"] == [(b"\xff\xd8cached", "jpeg")]
    assert not f.exists()


@pytest.mark.unit
def test_consume_images_redownloads_when_file_vanished(tmp_path):
    c, urls = _image_conn(tmp_path)
    db_mod.set_image_path(c, "mi", urls[0], str(tmp_path / "gone.jpg"))

    def download(conn, msg_id):
        f = tmp_path / "new.jpg"
        f.write_bytes(b"\x89PNG\r\n\x1a\nfresh")
        db_mod.set_image_path(conn, msg_id, urls[0], str(f))

    assert tools.consume_images(c, "mi", download)["images"] == [(b"\x89PNG\r\n\x1a\nfresh", "png")]


@pytest.mark.unit
def test_consume_images_errors(tmp_path):
    c, _ = _image_conn(tmp_path)
    assert "error" in tools.consume_images(c, "unknown", lambda *a: None)
    out = tools.consume_images(c, "mi", lambda *a: None)
    assert "error" in out and "images" not in out

    def boom(conn, msg_id):
        raise RuntimeError("no login")

    assert "no login" in tools.consume_images(c, "mi", boom)["error"]


@pytest.fixture()
def named_conn():
    c = db_mod.connect(":memory:")
    db_mod.upsert_messages(c, [
        _msg("n1", chat="48:notes", sender="Me", ts=10, text="note"),
        _msg("g1", chat="19:g@thread.v2", sender="Bob", ts=20, text="hi"),
        _msg("g2", chat="19:g@thread.v2", sender="Me", ts=30, text="yo"),
        _msg("g3", chat="19:g@thread.v2", sender="Carol", ts=40, text="hey"),
        _msg("s1", chat="19:s@thread.v2", sender="Dan", ts=50, text="named chat"),
    ])
    db_mod.set_chat_name(c, "19:s@thread.v2", "Sigma Team", "group")
    yield c
    c.close()


@pytest.mark.unit
def test_list_chats_names_unnamed_chats_from_participants(named_conn):
    names = {c["chat_id"]: c["display_name"] for c in tools.list_chats(named_conn)}
    assert names["48:notes"] == "Notes (self)"
    assert names["19:g@thread.v2"] == "Bob, Carol"  # self excluded, like Teams does
    assert names["19:s@thread.v2"] == "Sigma Team"


@pytest.mark.unit
def test_resolve_chat_by_participant_fallback_name(named_conn):
    assert tools.resolve_chat(named_conn, "Carol") == "19:g@thread.v2"
    assert tools.resolve_chat(named_conn, "notes") == "48:notes"


@pytest.mark.unit
def test_messages_carry_chat_name_and_readable_time(named_conn):
    (msg,) = [m for m in tools.recent(named_conn) if m["msg_id"] == "s1"]
    assert msg["chat_name"] == "Sigma Team"
    assert msg["time"] is not None and msg["time"].startswith("1970-01-01")


@pytest.mark.unit
def test_read_chat_defaults_to_latest_messages(named_conn):
    out = tools.read_chat(named_conn, "19:g@thread.v2", limit=2)
    assert [m["msg_id"] for m in out["messages"]] == ["g2", "g3"]
    assert out["total"] == 3 and out["truncated"] is True
    since = tools.read_chat(named_conn, "19:g@thread.v2", since=15, limit=1)
    assert [m["msg_id"] for m in since["messages"]] == ["g1"]


@pytest.mark.unit
def test_resolve_chat_prefers_exact_then_real_titles(named_conn):
    db_mod.upsert_messages(named_conn, [_msg("t1", chat="19:t@thread.v2", sender="Eve", ts=5, text="x")])
    db_mod.set_chat_name(named_conn, "19:t@thread.v2", "Bob Team")
    # "Bob" is in the newer untitled "Bob, Carol" chat, but a real title wins.
    assert tools.resolve_chat(named_conn, "bob") == "19:t@thread.v2"
    assert tools.resolve_chat(named_conn, "bob, carol") == "19:g@thread.v2"


@pytest.mark.unit
@pytest.mark.parametrize("data,fmt", [
    (b"\xff\xd8\xff\xe0rest", "jpeg"),
    (b"\x89PNG\r\n\x1a\nrest", "png"),
    (b"GIF89arest", "gif"),
    (b"RIFF1234WEBPrest", "webp"),
    (b"<!DOCTYPE html>", None),
])
def test_image_format_sniffs_magic_bytes(data, fmt):
    assert tools.image_format(data) == fmt


@pytest.mark.unit
def test_consume_images_rejects_non_image_bytes(tmp_path):
    c, urls = _image_conn(tmp_path)
    f = tmp_path / "x.jpg"
    f.write_bytes(b"<html>login</html>")
    db_mod.set_image_path(c, "mi", urls[0], str(f))
    out = tools.consume_images(c, "mi", lambda *a: None)
    assert "error" in out and "not an image" in out["error"]
    assert not f.exists()
