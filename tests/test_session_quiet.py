import pytest

from teams_reader.browser import session


@pytest.mark.unit
@pytest.mark.parametrize("method,url,blocked", [
    ("PUT", "https://t/api/chatsvc/emea/v1/users/ME/conversations/19:a/properties?name=consumptionhorizon", True),
    ("POST", "https://t/ups/emea/v1/me/reportmyactivity/", True),
    ("PUT", "https://t/v1/users/ME/endpoints/abc/presenceDocs/messagingService", True),
    ("POST", "https://t/ups/emea/v1/me/forceavailability/", True),
    ("GET", "https://t/api/chatsvc/emea/v1/users/ME/conversations/19:a/properties?name=consumptionhorizon", False),
    ("POST", "https://t/ups/emea/v1/presence/getpresence/", False),
    ("GET", "https://t/api/chatsvc/emea/v1/users/ME/conversations/19:a/messages", False),
])
def test_read_receipts_and_presence_writes_are_blocked(method, url, blocked):
    assert session.blocks_write(method, url) is blocked


class _Resp:
    def __init__(self, status, payload=None):
        self.status, self.ok, self._payload = status, 200 <= status < 300, payload

    def json(self):
        return self._payload


class _Request:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        return self.resp


class _Context:
    def __init__(self, resp):
        self.request = _Request(resp)


@pytest.mark.unit
def test_api_fetch_returns_json_or_an_error_payload():
    ok = _Context(_Resp(200, {"messages": []}))
    assert session.fetch_api(ok, "u", {"h": "1"}) == {"messages": []}
    assert ok.request.calls == [("u", {"h": "1"})]
    assert session.fetch_api(_Context(_Resp(401)), "u", {}) == {"error": 401}


@pytest.mark.unit
def test_api_fetch_network_failure_is_an_error_payload():
    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED")

    ctx = _Context(None)
    ctx.request = _Boom()
    assert "ERR_SOCKS" in session.fetch_api(ctx, "u", {})["error"]


@pytest.mark.unit
def test_hidden_browser_without_credentials_needs_an_explicit_login():
    assert session.sign_in_blocker(require_session=False, autologin=None, can_prompt=False)
    assert session.sign_in_blocker(require_session=True, autologin=object(), can_prompt=True)
    assert session.sign_in_blocker(require_session=False, autologin=object(), can_prompt=False) is None
    assert session.sign_in_blocker(require_session=False, autologin=None, can_prompt=True) is None


@pytest.mark.unit
@pytest.mark.parametrize("since_reload_ms,idle_ms,reload", [
    (100_000, 31_000, True),   # nothing has loaded for a while: reload
    (100_000, 5_000, False),   # a slow VPN still downloading: let it finish
    (50_000, 60_000, False),   # stalled, but reloaded too recently
])
def test_reload_only_when_the_page_has_stalled(since_reload_ms, idle_ms, reload):
    assert session.should_reload(since_reload_ms, idle_ms) is reload


@pytest.mark.unit
def test_open_timeout_defaults_and_profile_override():
    assert session.open_timeout_ms(interactive=True, load_timeout_s=None) == 180_000
    assert session.open_timeout_ms(interactive=False, load_timeout_s=None) == 75_000
    assert session.open_timeout_ms(interactive=True, load_timeout_s=420) == 420_000
    assert session.open_timeout_ms(interactive=False, load_timeout_s=420) == 420_000
