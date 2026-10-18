import pytest

from teams_reader.parse.conversations import resolve_names


# --- dict-form records (reliable chatTitle/topic/threadTopic only) ---

@pytest.mark.unit
def test_resolve_group_topic_dict():
    values = [{"id": "19:abc@thread.v2", "topic": "Project Sigma"}]
    assert resolve_names(values) == {"19:abc@thread.v2": ("Project Sigma", "group")}


@pytest.mark.unit
def test_resolve_chattitle_dict_kind_chat():
    values = [{"conversationId": "19:xyz@unq.gbl.spaces", "chatTitle": "Alice Nguyen"}]
    assert resolve_names(values)["19:xyz@unq.gbl.spaces"] == ("Alice Nguyen", "chat")


@pytest.mark.unit
def test_generic_name_keys_are_ignored():
    # 'name'/'title' hold mentions and roles like "Everyone" -> must not win.
    values = [{"id": "19:abc@thread.v2", "name": "Everyone", "title": "Everyone"}]
    assert resolve_names(values) == {}


@pytest.mark.unit
def test_known_ids_filters_unknown_chats():
    values = [{"id": "19:known@thread.v2", "chatTitle": "Keep"},
              {"id": "19:other@thread.v2", "chatTitle": "Drop"}]
    assert resolve_names(values, known_ids={"19:known@thread.v2"}) == {"19:known@thread.v2": ("Keep", "group")}


# --- raw serialized thread blobs (literal \xHH escapes) ---

def _blob(thread_id: str, fields: str) -> str:
    return f'o"\\x02id"-{thread_id}"\\x0AthreadType"\\x04chat{fields}'


@pytest.mark.unit
def test_blob_topic_with_control_length_prefix():
    # topic length 10 (0x0A) -> rendered as an escape before the value.
    blob = _blob("19:t1@thread.v2", '"\\x05topic"\\x0ATeam Alpha"\\x0AisMigrated')
    assert resolve_names([blob])["19:t1@thread.v2"] == ("Team Alpha", "group")


@pytest.mark.unit
def test_blob_topic_with_printable_length_prefix():
    # A 36-char title has length 0x24 == '$', a literal char, not an escape.
    name = "Onboarding CBU Acceleration Team XYZ"  # 36 chars
    assert len(name) == 36
    blob = _blob("19:t2@thread.v2", f'"\\x05topic"${name}"\\x0AisMigrated')
    assert resolve_names([blob])["19:t2@thread.v2"] == (name, "group")


@pytest.mark.unit
def test_blob_topic_wins_over_shorttitle():
    blob = _blob(
        "19:t3@thread.v2",
        '"\\x05topic"\\x08Real Name"\\x09chatTitle"\\x0AshortTitle"\\x05A,B,C"',
    )
    assert resolve_names([blob])["19:t3@thread.v2"][0] == "Real Name"


@pytest.mark.unit
def test_blob_shorttitle_fallback_when_no_topic():
    blob = _blob("19:t4@thread.v2", '"\\x0AshortTitle"\\x0BDas, Eli, +3"')
    assert resolve_names([blob])["19:t4@thread.v2"] == ("Das, Eli, +3", "group")


@pytest.mark.unit
def test_blob_unescapes_accented_bytes():
    # "Conceição" with ç=\xE7, ã=\xE3 as latin-1 escapes.
    blob = _blob("19:t5@thread.v2", '"\\x0AshortTitle"\\x09Concei\\xE7\\xE3o"')
    assert resolve_names([blob])["19:t5@thread.v2"][0] == "Conceição"


@pytest.mark.unit
def test_blob_filtered_by_known_ids():
    blob = _blob("19:t6@thread.v2", '"\\x05topic"\\x04Team"')
    assert resolve_names([blob], known_ids={"19:other@thread.v2"}) == {}


@pytest.mark.unit
def test_activity_record_names_via_source_thread():
    # The record's own id is a notifications thread; the real chat + name come
    # from sourceThreadId + sourceThreadTopic.
    blob = (
        'o"\\x02id"-19:teamsstream_notifications_x@thread.v2"'
        '"\\x0EsourceThreadId"-19:realgroup@thread.v2"'
        '"\\x11sourceThreadTopic"\\x07VN Team"'
    )
    out = resolve_names([blob])
    assert out.get("19:realgroup@thread.v2") == ("VN Team", "group")
    assert "19:teamsstream_notifications_x@thread.v2" not in out
