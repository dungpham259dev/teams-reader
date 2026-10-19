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
