import pytest

from teams_reader import settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    # Never touch the user's real config or data, or launch the real warm browser.
    monkeypatch.setenv("TEAMS_READER_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("TEAMS_READER_LIVE", "0")
    monkeypatch.setattr(settings, "APP_SUPPORT", tmp_path / "app-support")
    settings.activate(None)
    yield
    settings.activate(None)
