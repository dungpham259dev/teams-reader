import pytest

from teams_reader.browser import backfill, chatsvc


def _page(n, start, backward=None):
    msgs = [
        {
            "id": str(start + i),
            "content": f"msg {start + i}",
            "imdisplayname": "Alice",
            "originalarrivaltime": "2024-01-01T00:00:00.000Z",
        }
        for i in range(n)
    ]
    payload = {"messages": msgs}
    if backward is not None:
        payload["_metadata"] = {"backwardLink": backward}
    return payload


@pytest.mark.unit
def test_collect_follows_backward_links_until_exhausted():
    pages = {
        "p1": _page(2, 1, backward="p2"),
        "p2": _page(2, 3, backward="p3"),
        "p3": _page(1, 5),  # no backward link -> stop
    }
    result = backfill.collect_messages("p1", pages.get, chat_id="19:c@thread.v2")
    assert result.pages == 3
    assert [m.text for m in result.messages] == ["msg 1", "msg 2", "msg 3", "msg 4", "msg 5"]
    assert {m.chat_id for m in result.messages} == {"19:c@thread.v2"}


@pytest.mark.unit
def test_collect_respects_max_pages():
    pages = {"p1": _page(1, 1, backward="p2"), "p2": _page(1, 2, backward="p3")}
    result = backfill.collect_messages("p1", pages.get, chat_id="c", max_pages=1)
    assert result.pages == 1
    assert len(result.messages) == 1


@pytest.mark.unit
def test_collect_stops_on_empty_fetch():
    result = backfill.collect_messages("p1", lambda _u: None, chat_id="c", max_pages=5)
    assert result.pages == 1
    assert result.messages == []


@pytest.mark.unit
def test_collect_stops_on_fetch_error_payload():
    result = backfill.collect_messages("p1", lambda _u: {"error": 401}, chat_id="c")
    assert result.messages == []
    assert result.pages == 1


@pytest.mark.unit
def test_chat_targets_filters_by_name_or_id():
    from teams_reader.parse.messages import Message
    from teams_reader.store import db

    conn = db.connect(":memory:")
    db.upsert_messages(
        conn,
        [
            Message("m1", "19:a@thread.v2", "Alice", 1, "hi", None),
            Message("m2", "19:b@thread.v2", "Bob", 2, "yo", None),
        ],
    )
    db.set_chat_name(conn, "19:a@thread.v2", "Project Sigma")
    db.set_chat_name(conn, "19:b@thread.v2", "Lunch")

    assert {c for c, _ in backfill._chat_targets(conn, None)} == {
        "19:a@thread.v2",
        "19:b@thread.v2",
    }
    assert backfill._chat_targets(conn, "Sigma") == [("19:a@thread.v2", "Project Sigma")]
    assert backfill._chat_targets(conn, "19:b@thread.v2") == [("19:b@thread.v2", "Lunch")]
    conn.close()


def _page_ts(ids_ts, backward=None):
    payload = {"messages": [
        {"id": str(i), "content": f"m{i}", "imdisplayname": "A", "originalarrivaltime": ts}
        for i, ts in ids_ts
    ]}
    if backward:
        payload["_metadata"] = {"backwardLink": backward}
    return payload


@pytest.mark.unit
def test_collect_stops_paging_once_known_history_is_reached():
    pages = {
        "p1": _page_ts([(3, "2026-01-03T00:00:00Z"), (2, "2026-01-02T00:00:00Z")], backward="p2"),
        "p2": _page_ts([(1, "2026-01-01T00:00:00Z")], backward="p3"),
    }
    known = 1767312000000  # 2026-01-02T00:00:00Z
    result = backfill.collect_messages("p1", pages.get, chat_id="c", stop_at_ts=known)
    assert result.pages == 1
    assert [m.msg_id for m in result.messages] == ["3", "2"]


@pytest.mark.unit
def test_plan_targets_only_changed_or_new_chats():
    convs = [
        chatsvc.Conversation("19:same", "Same", "group", 100),
        chatsvc.Conversation("19:changed", "Changed", "group", 300),
        chatsvc.Conversation("19:new", None, "chat", 50),
        chatsvc.Conversation("19:noactivity", None, "chat", None),
    ]
    marks = {"19:same": 100, "19:changed": 200}
    idle = backfill.plan_targets([chatsvc.Conversation("19:idle", None, "meeting", None)], {"19:idle": 0})
    assert idle == []  # synced once, server reports no activity -> nothing to fetch
    names = {"19:new": "Bob"}
    plan = backfill.plan_targets(convs, marks, names=names)
    assert [(t.chat_id, t.name, t.stop_at_ts) for t in plan] == [
        ("19:changed", "Changed", 200),
        ("19:new", "Bob", None),
        ("19:noactivity", "19:noactivity", None),
    ]
    full = backfill.plan_targets(convs, marks, names=names, full=True)
    assert [t.chat_id for t in full] == ["19:same", "19:changed", "19:new", "19:noactivity"]
    assert all(t.stop_at_ts is None for t in full)
    only = backfill.plan_targets(convs, marks, names=names, chat_filter="bob", full=True)
    assert [t.chat_id for t in only] == ["19:new"]


@pytest.mark.unit
def test_apply_conversations_sets_titles_and_kinds_without_erasing_names():
    from teams_reader.store import db

    conn = db.connect(":memory:")
    db.set_chat_name(conn, "19:old", "Cache Name", "group")
    backfill.apply_conversations(conn, [
        chatsvc.Conversation("19:old", None, "chat", 1),
        chatsvc.Conversation("19:t", "VN - internal", "group", 2),
    ])
    rows = {r["chat_id"]: (r["display_name"], r["kind"]) for r in db.list_chats(conn)}
    assert rows["19:old"] == ("Cache Name", "chat")
    assert rows["19:t"] == ("VN - internal", "group")
    db.set_chat_watermark(conn, "19:t", 2)
    assert db.chat_watermarks(conn) == {"19:t": 2}
