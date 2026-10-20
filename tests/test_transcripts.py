import pytest

from teams_reader.browser import transcripts as fetch_mod
from teams_reader.mcp import tools
from teams_reader.parse import transcripts
from teams_reader.store import db

_AMS = "https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/0-weu-d5-abc/views/transcript"
_CHAT = "19:meeting_abc@thread.v2"


def _recording(ams=_AMS, title="Weekly sync", call_id="call-1", stamp="2026-10-06T08:43:31.21Z"):
    content = (
        '<URIObject type="Video.2/CallRecording.1"><Title>' + title + "</Title>"
        '<Identifiers><Id type="callId" value="' + call_id + '" /></Identifiers>'
        '<RecordingContent contentTypes="Transcript" timestamp="' + stamp + '" duration="0:26:00.6050727">'
        '<item type="amsVideo" uri="https://h/v1/objects/0-weu-d5-abc/views/video" />'
        '<item type="amsTranscript" uri="' + ams + '" />'
        '<item type="onedriveForBusinessTranscript" uri="https://x-my.sharepoint.com/_api/t/content" />'
        "</RecordingContent></URIObject>"
    )
    return {"messagetype": "RichText/Media_CallRecording", "content": content}


_VTT = """WEBVTT

00:00:06.621 --> 00:00:09.421
<v Ana Lee, Acme (External)>Can you reshare your screen?</v>

00:00:09.461 --> 00:00:09.581
<v Ana Lee, Acme (External)>Yeah.</v>

00:01:30.621 --> 00:01:35.021
<v Bo Chen, Acme>This is what you will work on &amp; ship.</v>

01:02:03.000 --> 01:02:04.000
No speaker here.
"""


@pytest.mark.unit
def test_extract_refs_reads_the_ams_transcript_of_a_recording_card():
    refs = transcripts.extract_refs([_recording(), {"messagetype": "RichText/Html", "content": "hi"}])
    assert len(refs) == 1
    ref = refs[0]
    assert ref.url == _AMS
    assert ref.call_id == "call-1"
    assert ref.title == "Weekly sync"
    assert ref.started_ts == 1791276211210
    assert ref.duration_s == 1560


@pytest.mark.unit
def test_extract_refs_dedupes_chunks_and_skips_cards_without_transcript():
    no_transcript = {"messagetype": "RichText/Media_CallRecording", "content": "<URIObject><Title>x</Title></URIObject>"}
    refs = transcripts.extract_refs([_recording(), _recording(), no_transcript])
    assert [r.url for r in refs] == [_AMS]


@pytest.mark.unit
def test_parse_vtt_keeps_time_speaker_and_unescaped_text():
    cues = transcripts.parse_vtt(_VTT)
    assert cues[0] == transcripts.Cue(6, "Ana Lee", "Can you reshare your screen?")
    assert cues[2] == transcripts.Cue(90, "Bo Chen", "This is what you will work on & ship.")
    assert cues[3] == transcripts.Cue(3723, "", "No speaker here.")


@pytest.mark.unit
def test_render_merges_consecutive_turns_of_one_speaker():
    text = transcripts.render(transcripts.parse_vtt(_VTT))
    assert text.splitlines() == [
        "[00:06] Ana Lee: Can you reshare your screen? Yeah.",
        "[01:30] Bo Chen: This is what you will work on & ship.",
        "[1:02:03] No speaker here.",
    ]


@pytest.mark.unit
def test_meeting_chat_ids_are_recognised():
    assert transcripts.is_meeting_chat(_CHAT)
    assert not transcripts.is_meeting_chat("19:abc_def@unq.gbl.spaces")


class _Resp:
    def __init__(self, status, body=b""):
        self.status, self._body = status, body

    def body(self):
        return self._body


class _Request:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        return self.responses[url]


class _Context:
    def __init__(self, responses):
        self.request = _Request(responses)


def _page(messages, backward=None):
    payload = {"messages": messages, "_metadata": {}}
    if backward:
        payload["_metadata"]["backwardLink"] = backward
    return payload


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "s.db")
    db.set_chat_name(connection, _CHAT, "Weekly sync", kind="meeting")
    yield connection
    connection.close()


@pytest.mark.unit
def test_discover_pages_meeting_chats_and_stores_new_refs(conn):
    template = "https://t/api/chatsvc/emea/v1/users/ME/conversations/TEMPLATE/messages?pageSize=200"
    first = "https://t/api/chatsvc/emea/v1/users/ME/conversations/19%3Ameeting_abc%40thread.v2/messages?pageSize=200"
    pages = {first: _page([{"messagetype": "Text", "content": "x"}], backward="https://t/older"),
             "https://t/older": _page([_recording()])}
    template = template.replace("TEMPLATE", "19%3Aother")
    found = fetch_mod.discover_with(conn, pages.__getitem__, template)
    assert found == 1
    assert [r["url"] for r in db.list_transcripts(conn)] == [_AMS]
    assert fetch_mod.discover_with(conn, pages.__getitem__, template) == 0


@pytest.mark.unit
def test_download_stores_rendered_text_and_marks_missing(conn):
    gone = _AMS.replace("abc", "old")
    db.add_transcript_ref(conn, _CHAT, transcripts.extract_refs([_recording()])[0])
    db.add_transcript_ref(conn, _CHAT, transcripts.extract_refs([_recording(ams=gone, call_id="c2")])[0])
    context = _Context({_AMS: _Resp(200, _VTT.encode()), gone: _Resp(404)})
    assert fetch_mod.download_with(conn, context) == {"requested": 2, "downloaded": 1, "unavailable": 1}
    rows = {r["url"]: r for r in db.list_transcripts(conn)}
    assert rows[_AMS]["status"] == "ok"
    assert rows[gone]["status"] == "unavailable"
    assert fetch_mod.download_with(conn, context) == {"requested": 0, "downloaded": 0, "unavailable": 0}


@pytest.mark.unit
def test_download_raises_auth_expired_on_401(conn):
    from teams_reader.browser.live import AuthExpired

    db.add_transcript_ref(conn, _CHAT, transcripts.extract_refs([_recording()])[0])
    with pytest.raises(AuthExpired):
        fetch_mod.download_with(conn, _Context({_AMS: _Resp(401)}))


@pytest.mark.unit
def test_tools_list_and_read_transcripts(conn):
    db.add_transcript_ref(conn, _CHAT, transcripts.extract_refs([_recording()])[0])
    fetch_mod.download_with(conn, _Context({_AMS: _Resp(200, _VTT.encode())}))
    listed = tools.list_transcripts(conn)
    assert listed[0]["chat"] == "Weekly sync"
    assert listed[0]["status"] == "ok"
    assert listed[0]["speakers"] == ["Ana Lee", "Bo Chen"]
    full = tools.read_transcript(conn, listed[0]["transcript_id"])
    assert full["text"].startswith("[00:06] Ana Lee:")
    assert full["truncated"] is False
    short = tools.read_transcript(conn, listed[0]["transcript_id"], max_chars=20)
    assert short["truncated"] is True and len(short["text"]) == 20
    assert "error" in tools.read_transcript(conn, 999)
    assert tools.list_transcripts(conn, chat="nope") == {"error": "chat not found: nope"}


@pytest.mark.unit
def test_sync_with_discovers_then_downloads(conn):
    class _Session:
        template_url = "https://t/api/chatsvc/emea/v1/users/ME/conversations/19%3Aother/messages?pageSize=200"
        context = _Context({_AMS: _Resp(200, _VTT.encode())})

        def fetch(self, url):
            return _page([_recording()])

    assert fetch_mod.sync_with(conn, _Session()) == {"new": 1, "requested": 1, "downloaded": 1, "unavailable": 0}


@pytest.mark.unit
def test_refs_only_trust_teams_transcript_hosts():
    evil = _recording(ams="https://attacker.example/v1/objects/x/views/transcript")
    plain = _recording(ams="http://eu-prod.asyncgw.teams.microsoft.com/v1/objects/x/views/transcript")
    assert transcripts.extract_refs([evil, plain]) == []


@pytest.mark.unit
def test_parse_vtt_splits_two_voices_in_one_cue():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v A, X>yes</v><v B, Y>no</v>\n"
    assert transcripts.parse_vtt(vtt) == [transcripts.Cue(1, "A", "yes"), transcripts.Cue(1, "B", "no")]


@pytest.mark.unit
def test_speakers_ignore_unattributed_text_with_a_colon():
    cues = [transcripts.Cue(0, "", "Note: buy milk"), transcripts.Cue(5, "Ana", "hi")]
    assert transcripts.cue_speakers(cues) == ["Ana"]


@pytest.mark.unit
def test_failed_first_scan_is_retried_in_full(conn):
    template = "https://t/api/chatsvc/emea/v1/users/ME/conversations/19%3Aother/messages?pageSize=200"
    assert fetch_mod.discover_with(conn, lambda url: {"error": 500}, template) == 0
    calls = []

    def fetch(url):
        calls.append(url)
        return _page([], backward=f"https://t/p{len(calls)}")

    fetch_mod.discover_with(conn, fetch, template)
    assert len(calls) == fetch_mod._FIRST_SCAN_PAGES


@pytest.mark.unit
def test_download_stops_the_batch_on_a_network_error(conn):
    second = _AMS.replace("abc", "def")
    db.add_transcript_ref(conn, _CHAT, transcripts.extract_refs([_recording()])[0])
    db.add_transcript_ref(conn, _CHAT, transcripts.extract_refs([_recording(ams=second, call_id="c2")])[0])

    class _Down:
        calls = 0

        def get(self, url, timeout=None):
            _Down.calls += 1
            raise TimeoutError("proxy down")

    context = _Context({})
    context.request = _Down()
    assert fetch_mod.download_with(conn, context)["downloaded"] == 0
    assert _Down.calls == 1


@pytest.mark.unit
def test_read_transcript_clamps_negative_max_chars(conn):
    db.add_transcript_ref(conn, _CHAT, transcripts.extract_refs([_recording()])[0])
    fetch_mod.download_with(conn, _Context({_AMS: _Resp(200, _VTT.encode())}))
    tid = tools.list_transcripts(conn)[0]["transcript_id"]
    assert tools.read_transcript(conn, tid, max_chars=-5)["text"] == ""
