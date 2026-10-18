import pytest

from teams_reader.browser import images
from teams_reader.parse.messages import Message, extract_messages
from teams_reader.store import db


@pytest.mark.unit
def test_extract_image_urls_from_img_html():
    content = (
        '<div><img src="https://eu-api.asm.skype.com/v1/objects/0-weu-d10-abc/views/imgo" '
        'itemid="https://eu-api.asm.skype.com/v1/objects/0-weu-d10-abc"></div>'
    )
    assert images.extract_image_urls(content) == [
        "https://eu-api.asm.skype.com/v1/objects/0-weu-d10-abc/views/imgo"
    ]


@pytest.mark.unit
def test_extract_image_urls_dedupes_and_normalizes_to_imgo():
    content = (
        "a https://h/v1/objects/ID1/views/imgt "
        "b https://h/v1/objects/ID1/views/imgo "
        "c https://h/v1/objects/ID2"
    )
    assert images.extract_image_urls(content) == [
        "https://h/v1/objects/ID1/views/imgo",
        "https://h/v1/objects/ID2/views/imgo",
    ]


@pytest.mark.unit
def test_extract_image_urls_empty():
    assert images.extract_image_urls("") == []
    assert images.extract_image_urls("no images here") == []


@pytest.mark.unit
def test_message_carries_image_urls():
    value = {
        "content": '<img src="https://h/v1/objects/XYZ/views/imgo">',
        "imdisplayname": "Alice",
        "originalarrivaltime": "2024-01-01T00:00:00.000Z",
        "id": "m1",
        "conversationId": "19:c@thread.v2",
    }
    msgs = extract_messages([value])
    assert len(msgs) == 1
    assert msgs[0].image_urls == ("https://h/v1/objects/XYZ/views/imgo",)


@pytest.mark.unit
def test_image_only_message_is_kept():
    value = {
        "content": '<img src="https://h/v1/objects/XYZ/views/imgo">',
        "imdisplayname": "Alice",
        "originalarrivaltime": "2024-01-01T00:00:00.000Z",
        "id": "m2",
    }
    msgs = extract_messages([value])
    assert len(msgs) == 1
    assert msgs[0].text == ""
    assert msgs[0].image_urls == ("https://h/v1/objects/XYZ/views/imgo",)


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


@pytest.mark.unit
def test_upsert_stores_attachments(conn):
    msg = Message(
        "m1", "19:c@thread.v2", "Alice", 1, "see pic", None,
        image_urls=("https://h/v1/objects/A/views/imgo",),
    )
    db.upsert_messages(conn, [msg])
    rows = db.images_for_chat(conn, "19:c@thread.v2")
    assert [r["url"] for r in rows] == ["https://h/v1/objects/A/views/imgo"]
    assert rows[0]["local_path"] is None


@pytest.mark.unit
def test_set_image_path_and_pending(conn):
    msg = Message(
        "m1", "19:c@thread.v2", "Alice", 1, "pic", None,
        image_urls=("https://h/v1/objects/A/views/imgo",),
    )
    db.upsert_messages(conn, [msg])
    pending = db.pending_images(conn)
    assert pending == [("m1", "https://h/v1/objects/A/views/imgo")]

    db.set_image_path(conn, "m1", "https://h/v1/objects/A/views/imgo", "/tmp/a.jpg")
    assert db.pending_images(conn) == []
    assert db.images_for_chat(conn, "19:c@thread.v2")[0]["local_path"] == "/tmp/a.jpg"


@pytest.mark.unit
def test_pending_images_filters(conn):
    a = Message("ma", "19:a@thread.v2", "Al", 1, "x", None, image_urls=("https://h/v1/objects/A/views/imgo",))
    b = Message("mb", "19:b@thread.v2", "Bo", 2, "y", None, image_urls=("https://h/v1/objects/B/views/imgo",))
    db.upsert_messages(conn, [a, b])
    db.set_chat_name(conn, "19:a@thread.v2", "Project Sigma")

    assert db.pending_images(conn, chat_filter="Sigma") == [("ma", "https://h/v1/objects/A/views/imgo")]
    assert db.pending_images(conn, chat_filter="19:b@thread.v2") == [("mb", "https://h/v1/objects/B/views/imgo")]
    assert db.pending_images(conn, msg_ids=["mb"]) == [("mb", "https://h/v1/objects/B/views/imgo")]

    row = db.image_row(conn, "ma")
    assert row["chat_id"] == "19:a@thread.v2" and row["url"].endswith("/A/views/imgo")


@pytest.mark.unit
def test_images_for_msg_and_clear_image_path(conn):
    urls = ("https://h/v1/objects/A/views/imgo", "https://h/v1/objects/B/views/imgo")
    db.upsert_messages(conn, [Message("m1", "19:c@thread.v2", "Al", 1, "pics", None, image_urls=urls)])
    db.set_image_path(conn, "m1", urls[0], "/tmp/a.jpg")

    assert [r["url"] for r in db.images_for_msg(conn, "m1")] == list(urls)
    assert db.images_for_msg(conn, "nope") == []

    db.clear_image_path(conn, "m1", urls[0])
    assert set(db.pending_images(conn)) == {("m1", urls[0]), ("m1", urls[1])}
