"""Teams meeting transcripts: where a meeting chat points to them, and their text.

When a recorded or transcribed meeting ends, Teams posts a
``RichText/Media_CallRecording`` card in the meeting chat. Its ``<item
type="amsTranscript">`` is a WebVTT file on the AMS service, readable with the
same browser cookies as chat images. A long call posts one card per chunk, all
pointing at the same transcript.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

_RECORDING_TYPE = "RichText/Media_CallRecording"
_AMS_RE = re.compile(r'<item type="amsTranscript" uri="([^"]+)"')
_TITLE_RE = re.compile(r"<Title>(.*?)</Title>", re.DOTALL)
_CALL_ID_RE = re.compile(r'<Id type="callId" value="([^"]*)"')
_CONTENT_RE = re.compile(r'<RecordingContent\b[^>]*\btimestamp="([^"]+)"[^>]*\bduration="([^"]+)"')
_CUE_TIME_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.\d+\s+-->")
_VOICE_RE = re.compile(r"<v ([^>]*)>(.*?)(?:</v>|(?=<v )|$)", re.DOTALL)
# Only Teams' AMS hosts: the URL comes from message content and is fetched with the user's cookies.
_TRUSTED_URL_RE = re.compile(r"^https://[a-z0-9.-]+\.(?:asyncgw\.teams\.microsoft\.com|asm\.skype\.com)/")
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass(frozen=True)
class TranscriptRef:
    url: str
    call_id: str
    title: str
    started_ts: int | None
    duration_s: int | None


@dataclass(frozen=True)
class Cue:
    start_s: int
    speaker: str
    text: str


def is_meeting_chat(chat_id: str | None) -> bool:
    return (chat_id or "").startswith("19:meeting_")


def _to_ms(stamp: str) -> int | None:
    trimmed = re.sub(r"(\.\d{1,6})\d*", r"\1", stamp.replace("Z", "+00:00"))
    try:
        return int(datetime.fromisoformat(trimmed).timestamp() * 1000)
    except ValueError:
        return None


def _to_seconds(duration: str) -> int | None:
    parts = duration.split(".")[0].split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


def _ref(content: str) -> TranscriptRef | None:
    url = _AMS_RE.search(content)
    if not url or not _TRUSTED_URL_RE.match(html.unescape(url.group(1))):
        return None
    title = _TITLE_RE.search(content)
    call_id = _CALL_ID_RE.search(content)
    timing = _CONTENT_RE.search(content)
    return TranscriptRef(
        url=html.unescape(url.group(1)),
        call_id=call_id.group(1) if call_id else "",
        title=html.unescape(title.group(1)).strip() if title else "",
        started_ts=_to_ms(timing.group(1)) if timing else None,
        duration_s=_to_seconds(timing.group(2)) if timing else None,
    )


def extract_refs(messages: Iterable[Any]) -> list[TranscriptRef]:
    """Transcript references in raw chatsvc messages, one per transcript URL."""
    refs: dict[str, TranscriptRef] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("messagetype") != _RECORDING_TYPE:
            continue
        ref = _ref(str(message.get("content") or ""))
        if ref is not None:
            refs.setdefault(ref.url, ref)
    return list(refs.values())


def _clean(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text)).strip()


def _cues(start_s: int, body: str) -> list[Cue]:
    voices = _VOICE_RE.findall(body)
    if not voices:
        return [Cue(start_s, "", _clean(body))]
    # "Ana Lee, Acme (External)": the part after the comma is the organisation.
    return [Cue(start_s, speaker.split(",")[0].strip(), _clean(text)) for speaker, text in voices]


def parse_vtt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [line for line in block.strip().split("\n") if line.strip()]
        stamp = next((i for i, line in enumerate(lines) if _CUE_TIME_RE.match(line)), None)
        if stamp is None:
            continue
        hours, minutes, seconds = (int(g) for g in _CUE_TIME_RE.match(lines[stamp]).groups())
        body = " ".join(lines[stamp + 1:])
        if body:
            cues.extend(c for c in _cues(hours * 3600 + minutes * 60 + seconds, body) if c.text)
    return cues


def _clock(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def render(cues: list[Cue]) -> str:
    """One line per speaker turn: ``[mm:ss] Name: text``."""
    turns: list[tuple[int, str, list[str]]] = []
    for cue in cues:
        if turns and turns[-1][1] == cue.speaker and cue.speaker:
            turns[-1][2].append(cue.text)
        else:
            turns.append((cue.start_s, cue.speaker, [cue.text]))
    return "\n".join(
        f"[{_clock(start)}] {speaker + ': ' if speaker else ''}{' '.join(parts)}"
        for start, speaker, parts in turns
    )


def cue_speakers(cues: list[Cue]) -> list[str]:
    """Distinct named speakers, in order of first cue."""
    return list(dict.fromkeys(c.speaker for c in cues if c.speaker))
