import threading

import pytest

from teams_reader.browser import backfill, live
from teams_reader.store import db as db_mod

TPL = "https://h/api/chatsvc/emea/v1/users/ME/conversations/__X__/messages?pageSize=200"


def _server(conv_time="2026-10-16T06:41:36Z"):
    convs = {"conversations": [{
        "id": "19:a@thread.v2",
        "threadProperties": {"topic": "Sigma", "productThreadType": "Chat"},
        "lastMessage": {"composetime": conv_time},
    }]}
    page = {"messages": [{
        "id": "1", "content": "hello", "imdisplayname": "Ann",
        "originalarrivaltime": "2026-10-16T06:41:36Z", "version": "1",
    }]}
    calls = []

    def fetch(url):
        calls.append(url)
        return convs if url.endswith("pageSize=500") else page

    return fetch, calls


@pytest.mark.unit
def test_sync_with_plans_from_server_list_and_is_incremental():
    conn = db_mod.connect(":memory:")
    fetch, calls = _server()
    first = backfill.sync_with(conn, fetch, TPL)
    assert (first.chats_synced, first.new_messages) == (1, 1)
    assert db_mod.list_chats(conn)[0]["display_name"] == "Sigma"
    calls.clear()
    second = backfill.sync_with(conn, fetch, TPL)
    assert second.chats_synced == 0 and len(calls) == 1  # only the conversation list


class _FakeSession:
    def __init__(self, log, fail_first_fetch=False):
        self.log = log
        self.fail = fail_first_fetch
        self.closed = False

    def fetch(self, url):
        if self.fail:
            self.fail = False
            return {"error": 401}
        return {"ok": url}

    def close(self):
        self.closed = True


@pytest.mark.unit
def test_live_browser_starts_once_on_one_thread_and_reuses_session():
    log, threads = [], set()

    def opener(interactive=False):
        log.append("open")
        return _FakeSession(log)

    browser = live.LiveBrowser(opener)
    try:
        def job(session):
            threads.add(threading.get_ident())
            return session.fetch("u")

        assert browser.run(job) == {"ok": "u"}
        assert browser.run(job) == {"ok": "u"}
        assert log == ["open"] and len(threads) == 1
        assert threading.get_ident() not in threads
    finally:
        browser.close()


@pytest.mark.unit
def test_live_browser_reopens_session_when_auth_expires():
    sessions = []

    def opener(interactive=False):
        sessions.append(_FakeSession([], fail_first_fetch=not sessions))
        return sessions[-1]

    browser = live.LiveBrowser(opener)
    try:
        assert browser.run(lambda s: live.checked(s.fetch("u"))) == {"ok": "u"}
        assert len(sessions) == 2 and sessions[0].closed
    finally:
        browser.close()


@pytest.mark.unit
def test_live_browser_surfaces_open_failures_and_retries_later():
    attempts = []

    def opener(interactive=False):
        attempts.append(1)
        raise RuntimeError("VPN down")

    browser = live.LiveBrowser(opener)
    try:
        with pytest.raises(RuntimeError, match="VPN down"):
            browser.run(lambda s: None)
        with pytest.raises(RuntimeError):
            browser.run(lambda s: None)
        assert len(attempts) == 2
    finally:
        browser.close()


@pytest.mark.unit
@pytest.mark.parametrize("now,last_use,last_sync,expected", [
    (100.0, 95.0, None, True),      # in use, never synced
    (100.0, 95.0, 80.0, False),     # synced 20s ago
    (100.0, 95.0, 60.0, True),      # synced 40s ago
    (2000.0, 95.0, 60.0, False),    # idle for > 15 min
    (100.0, None, None, False),     # never used
])
def test_should_sync_policy(now, last_use, last_sync, expected):
    assert live.should_sync(now, last_use, last_sync, interval=30, idle_after=900) is expected


def _paged_server(pages_needed=3, fail_with=None):
    convs = {"conversations": [{
        "id": "19:a@thread.v2", "threadProperties": {"productThreadType": "Chat"},
        "lastMessage": {"composetime": "2026-10-16T06:41:36Z"},
    }]}

    def page(n):
        body = {"messages": [{
            "id": str(100 - n), "content": f"m{n}", "imdisplayname": "Ann",
            "originalarrivaltime": f"2026-10-{16 - n:02d}T00:00:00Z", "version": "1",
        }]}
        if n + 1 < pages_needed:
            body["_metadata"] = {"backwardLink": f"page{n + 1}"}
        return body

    def fetch(url):
        if url.endswith("pageSize=500"):
            return convs
        if fail_with is not None:
            return {"error": fail_with}
        return page(int(url[4:]) if url.startswith("page") else 0)

    return fetch


@pytest.mark.unit
def test_capped_sync_does_not_advance_watermark():
    conn = db_mod.connect(":memory:")
    backfill.sync_with(conn, _paged_server(pages_needed=3), TPL, max_pages=2)
    assert db_mod.chat_watermarks(conn) == {}  # history incomplete: retry from the top
    summary = backfill.sync_with(conn, _paged_server(pages_needed=3), TPL, max_pages=5)
    assert "19:a@thread.v2" in db_mod.chat_watermarks(conn)
    assert summary.new_messages == 1  # counted from this sync's own inserts


@pytest.mark.unit
@pytest.mark.parametrize("error", [500, "TypeError: Failed to fetch"])
def test_failed_fetch_does_not_advance_watermark(error):
    conn = db_mod.connect(":memory:")
    backfill.sync_with(conn, _paged_server(fail_with=error), TPL)
    assert db_mod.chat_watermarks(conn) == {}


class _DyingSession(_FakeSession):
    def __init__(self):
        super().__init__([])
        self.dead = False

    def alive(self):
        return not self.dead


@pytest.mark.unit
def test_live_browser_reopens_a_crashed_session_but_keeps_a_healthy_one():
    sessions = []

    def opener(interactive=False):
        sessions.append(_DyingSession())
        return sessions[-1]

    browser = live.LiveBrowser(opener)
    try:
        def page_error(session):
            raise RuntimeError("net::ERR_CERT_AUTHORITY_INVALID")  # site problem, browser fine

        with pytest.raises(RuntimeError):
            browser.run(page_error)
        assert len(sessions) == 1

        def crash(session):
            session.dead = True
            raise RuntimeError("Target page, context or browser has been closed")

        with pytest.raises(RuntimeError):
            browser.run(crash)
        browser.run(lambda s: None)
        assert len(sessions) == 2
    finally:
        browser.close()


@pytest.mark.unit
def test_run_after_close_fails_fast():
    browser = live.LiveBrowser(lambda interactive=False: _FakeSession([]))
    browser.close()
    with pytest.raises(RuntimeError, match="closed"):
        browser.run(lambda s: None, timeout=2)


@pytest.mark.unit
def test_interactive_flag_reaches_the_opener():
    seen = []

    def opener(interactive=False):
        seen.append(interactive)
        if not interactive:
            raise live.LoginRequired("session expired")
        return _FakeSession([])

    browser = live.LiveBrowser(opener)
    try:
        with pytest.raises(live.LoginRequired):
            browser.run(lambda s: None)  # background: never logs in
        assert browser.run(lambda s: "ok", interactive=True) == "ok"
        assert seen == [False, True]
    finally:
        browser.close()


@pytest.mark.unit
def test_failed_open_closes_the_context_so_the_profile_lock_is_freed(monkeypatch):
    import playwright.sync_api as pw_api

    from teams_reader.browser import live as live_mod
    from teams_reader.browser import session as session_mod

    events = []

    class FakeContext:
        pages = []

        def close(self):
            events.append("context closed")

    class FakePlaywright:
        def start(self):
            return self

        def stop(self):
            events.append("playwright stopped")

    def no_capture(*args, **kwargs):
        raise TimeoutError("No chatsvc request seen")

    monkeypatch.setattr(pw_api, "sync_playwright", lambda: FakePlaywright())
    monkeypatch.setattr(session_mod, "launch_context", lambda pw, headless: FakeContext())
    monkeypatch.setattr(session_mod, "capture_request", no_capture)

    with pytest.raises(TimeoutError):
        live_mod.PlaywrightSession(interactive=False)
    assert events == ["context closed", "playwright stopped"]


@pytest.mark.unit
def test_failed_chats_are_counted_and_their_error_kept():
    conn = db_mod.connect(":memory:")
    summary = backfill.sync_with(conn, _paged_server(pages_needed=1, fail_with=429), TPL)
    assert summary.failed_chats == 1
    assert summary.last_error == "429"


@pytest.mark.unit
def test_a_clean_sync_reports_no_failures():
    conn = db_mod.connect(":memory:")
    fetch, _ = _server()
    summary = backfill.sync_with(conn, fetch, TPL)
    assert (summary.failed_chats, summary.last_error) == (0, None)
