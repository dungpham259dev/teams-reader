"""Lightweight heuristic profiles of a chat and its participants.

Pure statistics over a person's messages (no LLM): language mix, message
length, emoji and question rates. These seed tone for reply suggestions; richer
LLM-written profiles can be layered on later.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF✀-➿]"
)
# Characters that only appear in Vietnamese (quick language signal).
_VN_CHARS = set("ăâđêôơưàảãạáằẳẵặắầẩẫậấèẻẽẹéềểễệếìỉĩịíòỏõọóồổỗộốờởỡợớùủũụúừửữựứỳỷỹỵý"
                "ĂÂĐÊÔƠƯ")


@dataclass(frozen=True)
class Profile:
    sample_count: int
    language: str          # "vi", "en", or "mixed"
    avg_words: float
    emoji_rate: float      # fraction of messages with an emoji
    question_rate: float   # fraction of messages asking something
    style: str             # short human label


def _is_vietnamese(text: str) -> bool:
    return any(ch in _VN_CHARS for ch in text)


def _label(avg_words: float, emoji_rate: float, question_rate: float) -> str:
    length = "terse" if avg_words < 6 else ("long" if avg_words > 25 else "medium")
    bits = [length]
    if emoji_rate > 0.3:
        bits.append("emoji-heavy")
    if question_rate > 0.4:
        bits.append("asks-a-lot")
    return ", ".join(bits)


def build_profile(texts: list[str]) -> Profile:
    texts = [t for t in texts if t and t.strip()]
    n = len(texts)
    if n == 0:
        return Profile(0, "unknown", 0.0, 0.0, 0.0, "no data")

    vn = sum(1 for t in texts if _is_vietnamese(t))
    if vn >= n * 0.6:
        language = "vi"
    elif vn <= n * 0.1:
        language = "en"
    else:
        language = "mixed"

    avg_words = sum(len(_WORD_RE.findall(t)) for t in texts) / n
    emoji_rate = sum(1 for t in texts if _EMOJI_RE.search(t)) / n
    question_rate = sum(1 for t in texts if "?" in t) / n

    return Profile(
        sample_count=n,
        language=language,
        avg_words=round(avg_words, 1),
        emoji_rate=round(emoji_rate, 2),
        question_rate=round(question_rate, 2),
        style=_label(avg_words, emoji_rate, question_rate),
    )


def profile_dict(texts: list[str]) -> dict:
    return asdict(build_profile(texts))
