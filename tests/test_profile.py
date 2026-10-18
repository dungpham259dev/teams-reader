import pytest

from teams_reader.parse.profile import build_profile


@pytest.mark.unit
def test_empty_profile():
    p = build_profile([])
    assert p.sample_count == 0
    assert p.language == "unknown"


@pytest.mark.unit
def test_english_terse_profile():
    p = build_profile(["ok", "on it", "done thanks", "sure"])
    assert p.language == "en"
    assert p.avg_words < 6
    assert "terse" in p.style


@pytest.mark.unit
def test_vietnamese_detected():
    p = build_profile(["ừ để mình xem", "đã xong rồi nhé", "cảm ơn bạn nhiều"])
    assert p.language == "vi"


@pytest.mark.unit
def test_mixed_language():
    p = build_profile(["ok done", "ừ để mình xem nhé", "sure thing", "thanks"])
    assert p.language == "mixed"


@pytest.mark.unit
def test_emoji_and_question_rates():
    p = build_profile(["really? 😄", "why not? 🤔", "how?", "ok"])
    assert p.emoji_rate > 0.3
    assert p.question_rate > 0.4
    assert "asks-a-lot" in p.style
