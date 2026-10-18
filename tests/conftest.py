import pytest

from teams_reader import settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    # Never read the user's real config or launch the real warm browser from tests.
    monkeypatch.setenv("TEAMS_READER_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("TEAMS_READER_LIVE", "0")
    settings.activate(None)
    yield
    settings.activate(None)
