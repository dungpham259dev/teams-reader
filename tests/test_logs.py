import logging

import pytest

from teams_reader import logs, settings


@pytest.mark.unit
def test_setup_writes_to_the_profile_log(tmp_path):
    profile = settings.Profile(name="t", data_dir=tmp_path)
    path = logs.setup(profile)
    logs.get("mcp").info("hello %d", 3)
    for handler in logging.getLogger("teams_reader").handlers:
        handler.flush()
    assert path == tmp_path / "logs" / "teams-reader.log"
    line = path.read_text().strip()
    assert line.endswith("mcp        hello 3") and "INFO" in line


@pytest.mark.unit
def test_setup_twice_keeps_one_handler(tmp_path):
    profile = settings.Profile(name="t", data_dir=tmp_path)
    logs.setup(profile)
    logs.setup(profile)
    assert len(logging.getLogger("teams_reader").handlers) == 1


@pytest.mark.unit
def test_summaries_hold_sizes_not_content():
    assert logs.summarize([1, 2]) == "2 items"
    assert logs.summarize({"messages": [1, 2, 3], "total": 3}) == "messages=3"
    assert logs.summarize({"error": "chat not found: x"}) == "error: chat not found: x"
    assert "secret text" not in logs.summarize({"text": "secret text"})


@pytest.mark.unit
def test_short_args_truncates_and_drops_none():
    text = logs.short_args(("Sigma",), {"limit": 5, "since": None, "q": "x" * 100})
    assert text.startswith("'Sigma', limit=5, q='xxx") and "since" not in text and text.endswith("…")
