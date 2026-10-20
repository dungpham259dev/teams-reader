import pytest

from teams_reader.browser import chatsvc


def _page(messages, backward=None):
    payload = {"messages": messages}
    if backward is not None:
        payload["_metadata"] = {"backwardLink": backward}
    return payload


def _msg(text="hi", sender="Alice", ts="2024-01-01T00:00:00.000Z", mid="1"):
    return {
        "id": mid,
        "content": text,
        "imdisplayname": sender,
        "originalarrivaltime": ts,
    }


@pytest.mark.unit
def test_is_messages_url():
    url = "https://apac.ng.msg.teams.microsoft.com/v1/users/ME/conversations/19:abc@thread.v2/messages?pageSize=200"
    assert chatsvc.is_messages_url(url)
    assert not chatsvc.is_messages_url("https://teams.microsoft.com/v2/")
    assert not chatsvc.is_messages_url("")


@pytest.mark.unit
def test_conversation_of_decodes():
    url = "https://x/v1/users/ME/conversations/19%3Aabc%40thread.v2/messages?pageSize=50"
    assert chatsvc.conversation_of(url) == "19:abc@thread.v2"


@pytest.mark.unit
def test_with_conversation_reuses_host_and_query():
    template = "https://apac.ng.msg.teams.microsoft.com/v1/users/ME/conversations/19%3Aold%40thread.v2/messages?pageSize=200&view=msnp24Equivalent"
    out = chatsvc.with_conversation(template, "19:new@thread.v2")
    assert "19%3Anew%40thread.v2/messages" in out
    assert "old" not in out
    assert out.endswith("?pageSize=200&view=msnp24Equivalent")


@pytest.mark.unit
def test_with_conversation_rejects_non_messages_url():
    with pytest.raises(ValueError):
        chatsvc.with_conversation("https://teams.microsoft.com/v2/", "19:new@thread.v2")


@pytest.mark.unit
def test_is_conversations_url():
    assert chatsvc.is_conversations_url("https://apac.ng.msg.teams.microsoft.com/v1/users/ME/conversations?view=x")
    assert not chatsvc.is_conversations_url("https://teams.microsoft.com/v2/")


@pytest.mark.unit
def test_messages_template_from_conversations_url_fills_per_chat():
    conv = "https://apac.ng.msg.teams.microsoft.com/v1/users/ME/conversations?view=msnp24Equivalent&pageSize=50"
    template = chatsvc.messages_template(conv)
    assert chatsvc.is_messages_url(template)
    filled = chatsvc.with_conversation(template, "19:abc@thread.v2")
    assert "apac.ng.msg.teams.microsoft.com" in filled
    assert "19%3Aabc%40thread.v2/messages" in filled
    assert "__CID__" not in filled


@pytest.mark.unit
def test_messages_template_keeps_proxied_path_prefix():
    conv = "https://teams.microsoft.com/api/chatsvc/apac/v1/users/ME/conversations?pageSize=50"
    template = chatsvc.messages_template(conv)
    filled = chatsvc.with_conversation(template, "19:abc@thread.v2")
    assert filled.startswith("https://teams.microsoft.com/api/chatsvc/apac/v1/users/ME/conversations/")
    assert "19%3Aabc%40thread.v2/messages" in filled


@pytest.mark.unit
def test_messages_template_rejects_unrelated_url():
    with pytest.raises(ValueError):
        chatsvc.messages_template("https://teams.microsoft.com/v2/")


@pytest.mark.unit
def test_parse_page_extracts_messages_and_forces_chat_id():
    payload = _page([_msg(text="hello there", mid="1"), _msg(text="again", mid="2")])
    msgs = chatsvc.parse_page(payload, chat_id="19:abc@thread.v2")
    assert [m.text for m in msgs] == ["hello there", "again"]
    assert {m.chat_id for m in msgs} == {"19:abc@thread.v2"}


@pytest.mark.unit
def test_parse_page_without_chat_id_keeps_own():
    payload = _page([_msg(mid="1")])
    msgs = chatsvc.parse_page(payload)
    assert len(msgs) == 1


@pytest.mark.unit
def test_backward_link_variants_and_absent():
    assert chatsvc.backward_link(_page([], backward="https://next")) == "https://next"
    assert chatsvc.backward_link({"_metadata": {"backwardlink": "https://n2"}}) == "https://n2"
    assert chatsvc.backward_link(_page([])) is None
    assert chatsvc.backward_link({"_metadata": {}}) is None
    assert chatsvc.backward_link("not a dict") is None


def _conv(cid, topic=None, ptype="Chat", last="2026-10-16T06:41:36.298Z", last_id=None):
    conv = {"id": cid, "threadProperties": {"productThreadType": ptype}, "lastMessage": {}}
    if topic:
        conv["threadProperties"]["topic"] = topic
    if last:
        conv["lastMessage"]["composetime"] = last
    if last_id:
        conv["lastUpdatedMessageId"] = last_id
    return conv


@pytest.mark.unit
def test_conversations_url_from_template():
    tpl = "https://t.cloud.microsoft/api/chatsvc/emea/v1/users/ME/conversations/X/messages?pageSize=20"
    assert chatsvc.conversations_url(tpl) == (
        "https://t.cloud.microsoft/api/chatsvc/emea/v1/users/ME/conversations"
        "?view=msnp24Equivalent&pageSize=500"
    )


@pytest.mark.unit
def test_parse_conversations_maps_topic_kind_and_activity():
    payload = {"conversations": [
        _conv("19:g@thread.v2", topic="VN - internal"),
        _conv("19:a_b@unq.gbl.spaces", ptype="OneToOneChat", last=None, last_id=1792000000000),
        _conv("19:meeting_x@thread.v2", topic="[CBU] Sync", ptype="Meeting"),
        _conv("48:notes", ptype="StreamOfNotes"),
        _conv("48:notifications", ptype="StreamOfNotifications"),
    ]}
    convs = {c.chat_id: c for c in chatsvc.parse_conversations(payload)}
    assert set(convs) == {"19:g@thread.v2", "19:a_b@unq.gbl.spaces", "19:meeting_x@thread.v2", "48:notes"}
    assert convs["19:g@thread.v2"].topic == "VN - internal"
    assert convs["19:g@thread.v2"].kind == "group"
    assert convs["19:g@thread.v2"].last_activity_ms == 1792132896298
    assert convs["19:a_b@unq.gbl.spaces"].kind == "chat"
    assert convs["19:a_b@unq.gbl.spaces"].topic is None
    assert convs["19:a_b@unq.gbl.spaces"].last_activity_ms == 1792000000000
    assert convs["19:meeting_x@thread.v2"].kind == "meeting"
    assert chatsvc.parse_conversations({"error": 401}) == []


@pytest.mark.unit
def test_an_edit_or_delete_counts_as_activity():
    conv = _conv("48:notes", ptype="StreamOfNotes", last="2026-10-20T11:29:05.115Z")
    conv["lastUpdatedMessageVersion"] = 1792495798170  # a delete, 50s after the last message
    [parsed] = chatsvc.parse_conversations({"conversations": [conv]})
    assert parsed.last_activity_ms == 1792495798170
