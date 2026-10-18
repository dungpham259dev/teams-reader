import time

import pytest

from teams_reader.mcp import livesync


class _FakeBrowser:
    def __init__(self):
        self.jobs = 0
        self.closed = False

    def run(self, job, timeout=None, interactive=False):
        self.jobs += 1
        return job(_FakeSession())

    def close(self):
        self.closed = True


class _FakeSession:
    template_url = "tpl"
    context = object()

    def fetch(self, url):
        return {}


def _wait_for(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.unit
def test_background_sync_starts_on_first_use_and_runs(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(livesync, "sync_with", lambda conn, fetch, tpl, **kw: synced.append(tpl))
    browser = _FakeBrowser()
    live = livesync.LiveSync(tmp_path / "s.db", browser_factory=lambda: browser, tick=0.01, interval=0)
    try:
        assert synced == []  # nothing before the first tool call
        live.touch()
        assert _wait_for(lambda: synced)
        assert live.status()["last_error"] is None
    finally:
        live.close()
    assert browser.closed


@pytest.mark.unit
def test_background_sync_records_errors_and_keeps_going(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("VPN down")

    monkeypatch.setattr(livesync, "sync_with", boom)
    live = livesync.LiveSync(tmp_path / "s.db", browser_factory=_FakeBrowser, tick=0.01, interval=0)
    try:
        live.touch()
        assert _wait_for(lambda: live.status()["last_error"])
        assert "VPN down" in live.status()["last_error"]
    finally:
        live.close()


@pytest.mark.unit
def test_run_passes_session_and_worker_connection(tmp_path):
    live = livesync.LiveSync(tmp_path / "s.db", browser_factory=_FakeBrowser, tick=10)
    try:
        out = live.run(lambda session, conn: (session.template_url, conn.execute("SELECT 1").fetchone()[0]))
        assert out == ("tpl", 1)
    finally:
        live.close()


@pytest.mark.unit
def test_enabled_respects_env(monkeypatch):
    from teams_reader import settings

    monkeypatch.setenv("TEAMS_READER_LIVE", "0")
    settings.activate(None)
    assert livesync.enabled() is False
    monkeypatch.setenv("TEAMS_READER_LIVE", "1")
    settings.activate(None)
    assert livesync.enabled() is True


@pytest.mark.unit
@pytest.mark.parametrize("failures,expected", [(0, 30), (1, 60), (3, 240), (10, 600)])
def test_retry_backoff_after_failures(failures, expected):
    assert livesync.backoff(30, failures, cap=600) == expected
