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


def _holder(tmp_path, monkeypatch, sync=None):
    from teams_reader.store import db as db_mod

    synced = []
    monkeypatch.setattr(livesync, "sync_with", sync or (lambda conn, fetch, tpl, **kw: synced.append(tpl)))
    live = livesync.LiveSync(tmp_path / "s.db", browser_factory=_FakeBrowser, interval=30)
    return live, db_mod.connect(tmp_path / "s.db"), synced


@pytest.mark.unit
def test_a_question_from_another_session_triggers_a_sync_now(tmp_path, monkeypatch):
    from teams_reader.mcp import freshness

    live, conn, synced = _holder(tmp_path, monkeypatch)
    try:
        live._poll_once(conn)
        assert synced == []  # nobody asked anything yet
        freshness.mark_used(conn, time.time())  # written by another MCP process
        live._poll_once(conn)
        assert synced == ["tpl"]
        assert freshness.synced_at(conn) is not None
        live._poll_once(conn)
        assert synced == ["tpl"]  # answered; next one waits for the interval
    finally:
        live.close()


@pytest.mark.unit
def test_profile_in_use_is_not_reported_as_a_broken_sync(tmp_path, monkeypatch):
    from teams_reader.browser.profile_lock import ProfileInUse
    from teams_reader.mcp import freshness
    from teams_reader.store import db as db_mod

    def held(*a, **k):
        raise ProfileInUse("held by pid 1")

    live, conn, _ = _holder(tmp_path, monkeypatch, sync=held)
    try:
        freshness.mark_used(conn, time.time())
        live._poll_once(conn)
        assert db_mod.get_state(conn, freshness.ERROR_KEY) is None
        assert "held by pid 1" in live.status()["last_error"]
    finally:
        live.close()


@pytest.mark.unit
def test_a_real_failure_is_shared_so_other_sessions_do_not_wait(tmp_path, monkeypatch):
    from teams_reader.mcp import freshness
    from teams_reader.store import db as db_mod

    def expired(*a, **k):
        raise RuntimeError("Teams session expired")

    live, conn, _ = _holder(tmp_path, monkeypatch, sync=expired)
    try:
        freshness.mark_used(conn, time.time())
        live._poll_once(conn)
        assert "expired" in db_mod.get_state(conn, freshness.ERROR_KEY)
    finally:
        live.close()


@pytest.mark.unit
def test_lost_login_is_recovered_once_a_fresh_question_arrives(tmp_path, monkeypatch):
    from teams_reader.browser.live import LoginRequired

    calls = []

    def sync(conn, fetch, tpl, **kw):
        calls.append(kw)
        if len(calls) == 1:
            raise LoginRequired("sign-in page")

    monkeypatch.setattr(livesync, "sync_with", sync)
    browser = _FakeBrowser()
    interactive = []
    real_run = browser.run

    def run(job, timeout=None, interactive_flag=False, **kw):
        interactive.append(kw.get("interactive", interactive_flag))
        return real_run(job)

    browser.run = run
    live = livesync.LiveSync(tmp_path / "s.db", browser_factory=lambda: browser, tick=0.01, interval=1000)
    try:
        live.recover_login()  # nothing lost yet
        live.touch()
        assert _wait_for(lambda: live.status()["last_error"] == "sign-in page")
        live.recover_login()
        assert _wait_for(lambda: live.status()["last_error"] is None)
        live.recover_login()  # recovered: no further sign-in
        time.sleep(0.05)
        assert interactive == [False, True]
    finally:
        live.close()


@pytest.mark.unit
def test_a_question_already_answered_by_a_fresh_sync_does_not_sync_again(tmp_path):
    from teams_reader.mcp import freshness
    from teams_reader.store import db as db_mod

    live = livesync.LiveSync(tmp_path / "s.db", browser_factory=_FakeBrowser, interval=1000)
    conn = db_mod.connect(tmp_path / "s.db")
    try:
        now = time.monotonic()
        live._last_use = now
        live._last_sync = now - 10  # past the minimum gap, well inside the interval
        freshness.mark_used(conn, 1_000.0)
        assert live._due(conn, now)  # asked, nothing synced since
        freshness.mark_synced(conn, 1_000.5)
        assert not live._due(conn, now)
    finally:
        conn.close()
        live.close()
