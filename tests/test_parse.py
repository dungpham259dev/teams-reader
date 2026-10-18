import json

import pytest

from teams_reader.parse.decode import decode_stream, decoded_values, unwrap
from teams_reader.parse.messages import extract_messages
from tests.fixtures.synthetic import message_value, sample_dump


@pytest.mark.unit
def test_decode_stream_reads_jsonl_and_concatenated():
    jsonl = '{"a": 1}\n{"a": 2}'
    concatenated = '{"a": 1}{"a": 2}'
    assert [r["a"] for r in decode_stream(jsonl)] == [1, 2]
    assert [r["a"] for r in decode_stream(concatenated)] == [1, 2]


@pytest.mark.unit
def test_decode_stream_skips_malformed_lines():
    raw = '{"a": 1}\nnot json at all\n{"a": 2}'
    assert [r["a"] for r in decode_stream(raw)] == [1, 2]


@pytest.mark.unit
def test_decode_stream_trailing_whitespace_and_no_newline():
    assert list(decode_stream("   \n  ")) == []
    # A final unparseable line with no trailing newline stops cleanly.
    assert [r["a"] for r in decode_stream('{"a": 1}\ntail-no-newline')] == [1]


@pytest.mark.unit
def test_decoded_values_skips_records_without_value():
    raw = '{"object_store_id": 0}\n{"value": {"content": "x"}}'
    assert list(decoded_values(raw)) == [{"content": "x"}]


@pytest.mark.unit
def test_unwrap_keeps_plain_and_nonjson_strings():
    assert unwrap("just text") == "just text"
    # Looks like JSON (braces) but is invalid -> returned unchanged.
    assert unwrap("{bad}") == "{bad}"


@pytest.mark.unit
def test_unwrap_collapses_wrappers():
    wrapped = {"__type__": "X", "value": {"content": "hi"}}
    assert unwrap(wrapped) == {"content": "hi"}


@pytest.mark.unit
def test_unwrap_parses_nested_json_string():
    payload = {"content": "<p>x</p>", "imdisplayname": "A"}
    assert unwrap(json.dumps(payload)) == payload


@pytest.mark.unit
def test_extract_basic_message():
    values = list(decoded_values(sample_dump()))
    messages = extract_messages(values)
    by_id = {m.msg_id: m for m in messages}

    alice = by_id["m1"]
    assert alice.sender == "Alice"
    assert alice.text == "hello team"
    assert alice.quoted_text is None
    assert alice.ts == 1_787_000_000_000
    assert alice.chat_id == "19:demo@thread.v2"


@pytest.mark.unit
def test_extract_splits_blockquote_quote():
    message = {m.msg_id: m for m in extract_messages(decoded_values(sample_dump()))}["m2"]
    assert message.quoted_text == "hello team"
    assert message.text == "hi Alice, on it"


@pytest.mark.unit
def test_extract_skips_control_sender():
    ids = {m.msg_id for m in extract_messages(decoded_values(sample_dump()))}
    assert "c1" not in ids


@pytest.mark.unit
def test_extract_reads_double_encoded_message():
    message = {m.msg_id: m for m in extract_messages(decoded_values(sample_dump()))}["m3"]
    assert message.sender == "Carol"
    assert message.text == "deploying now"


@pytest.mark.unit
def test_extract_dedupes_by_id():
    one = message_value("dup", "A", "<p>a</p>", 1_787_000_000_000)
    values = [{"items": [one, dict(one)]}]
    assert len(extract_messages(values)) == 1


@pytest.mark.unit
def test_extract_hashes_id_when_missing():
    no_id = {
        "imdisplayname": "A",
        "content": "<p>no id here</p>",
        "originalarrivaltime": 1_787_000_000_000,
    }
    (message,) = extract_messages([no_id])
    assert message.msg_id.startswith("h:")


@pytest.mark.unit
def test_extract_handles_non_numeric_timestamp():
    node = {"id": "x", "imdisplayname": "A", "content": "<p>hi</p>", "originalarrivaltime": "not-a-number"}
    (message,) = extract_messages([node])
    assert message.ts is None


@pytest.mark.unit
def test_extract_skips_content_without_sender_or_time():
    # content present but neither sender nor timestamp -> not a message.
    assert extract_messages([{"content": "<p>orphan</p>"}]) == []


@pytest.mark.unit
def test_extract_normalizes_conversation_link_chat_id():
    node = {
        "id": "x",
        "imdisplayname": "A",
        "content": "<p>hi</p>",
        "originalarrivaltime": 1,
        "conversationLink": "conversation/19:abc@thread.v2;messageid=1789994097772",
    }
    (message,) = extract_messages([node])
    assert message.chat_id == "19:abc@thread.v2"


@pytest.mark.unit
def test_extract_skips_empty_content():
    node = {"id": "x", "imdisplayname": "A", "content": "<p></p>", "originalarrivaltime": 1}
    assert extract_messages([node]) == []


@pytest.mark.unit
def test_to_ms_parses_iso8601_and_epoch_ms():
    from teams_reader.parse.messages import _to_ms
    assert _to_ms(1_700_000_000_000) == 1_700_000_000_000
    assert _to_ms("2024-01-01T00:00:00.000Z") == 1704067200000
    assert _to_ms("not a date") is None
    assert _to_ms(None) is None
    assert _to_ms(1_700_000_000) is None  # 10-digit seconds are not ms


def _node(**over):
    base = {
        "id": "x1",
        "conversationid": "19:a@thread.v2",
        "messagetype": "RichText/Html",
        "content": "<p>hello</p>",
        "imdisplayname": "Alice",
        "originalarrivaltime": "2026-10-16T06:41:36.2980000Z",
    }
    base.update(over)
    return base


@pytest.mark.unit
def test_extract_skips_system_activity_messages():
    node = _node(messagetype="ThreadActivity/AddMember", content="<addmember>x</addmember>")
    assert extract_messages([node]) == []


@pytest.mark.unit
def test_extract_skips_url_and_worker_senders():
    url_sender = _node(id="u", imdisplayname=None, **{"from": "https://h/v1/users/ME/contacts/8:orgid:1"})
    worker = _node(id="w", imdisplayname="worker/8:orgid:abc")
    assert extract_messages([url_sender, worker]) == []


@pytest.mark.unit
def test_extract_reads_chatsvc_seven_digit_fraction():
    (msg,) = extract_messages([_node()])
    assert msg.ts == 1792132896298
