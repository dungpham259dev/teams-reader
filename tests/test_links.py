import pytest

from teams_reader.mcp import tools
from teams_reader.parse.links import extract_link_urls
from teams_reader.parse.messages import Message, extract_messages
from teams_reader.store import db as db_mod


@pytest.mark.unit
def test_extract_prefers_full_href_over_truncated_text():
    html = (
        '<p>spec: <a href="https://x.atlassian.net/wiki/pages/65/DRAFT+IV+Cluster" '
        'title="t">https://x.atlassian.net/wiki/pages/65/DRAFT+IV…</a></p>'
    )
    assert extract_link_urls(html) == ["https://x.atlassian.net/wiki/pages/65/DRAFT+IV+Cluster"]


@pytest.mark.unit
def test_extract_plain_urls_unescapes_and_dedupes():
    html = "<p>see https://lab.dev/a?x=1&amp;y=2, and https://lab.dev/a?x=1&amp;y=2.</p>"
    assert extract_link_urls(html) == ["https://lab.dev/a?x=1&y=2"]


@pytest.mark.unit
def test_extract_skips_teams_image_objects_and_mailto():
    html = (
        '<img src="https://eu-api.asm.skype.com/v1/objects/0-weu/views/imgo">'
        '<a href="mailto:a@b.c">mail</a><a href="https://ok.example/p">p</a>'
    )
    assert extract_link_urls(html) == ["https://ok.example/p"]


@pytest.mark.unit
def test_message_carries_links_and_store_lists_them():
    node = {
        "id": "m1", "conversationid": "19:a@thread.v2", "imdisplayname": "Ann",
        "originalarrivaltime": "2026-10-16T06:41:36Z",
        "content": '<p><a href="https://lab.dev/repo">https://lab.dev/re…</a></p>',
    }
    (msg,) = extract_messages([node])
    assert msg.link_urls == ("https://lab.dev/repo",)

    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(conn, [msg])
    db_mod.upsert_messages(conn, [msg])  # idempotent
    db_mod.set_chat_name(conn, "19:a@thread.v2", "Sigma")
    out = tools.list_links(conn, "Sigma")
    assert [(l["url"], l["host"], l["sender"]) for l in out["links"]] == [
        ("https://lab.dev/repo", "lab.dev", "Ann")
    ]
    assert tools.list_links(conn, "nope")["error"]


@pytest.mark.unit
def test_list_links_across_all_chats_filters_by_host():
    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(conn, [
        Message("a", "19:a", "Ann", 1, "x", None, link_urls=("https://lab.dev/1",)),
        Message("b", "19:b", "Bob", 2, "y", None, link_urls=("https://wiki.org/2",)),
    ])
    assert [l["url"] for l in tools.list_links(conn, None, host="lab.dev")["links"]] == ["https://lab.dev/1"]
    assert len(tools.list_links(conn, None)["links"]) == 2


def _store_with_link(url="https://wiki.example/page"):
    conn = db_mod.connect(":memory:")
    db_mod.upsert_messages(conn, [Message("a", "19:a", "Ann", 1, "x", None, link_urls=(url,))])
    return conn


@pytest.mark.unit
def test_read_link_only_reads_urls_shared_in_chats():
    conn = _store_with_link()
    calls = []
    out = tools.read_link("https://evil.example/x", lambda u: calls.append(u), lambda u: db_mod.link_shared(conn, u))
    assert "not shared" in out["error"] and calls == []


@pytest.mark.unit
def test_read_link_returns_text_and_truncates():
    conn = _store_with_link()
    page = {"final_url": "https://wiki.example/page", "title": "Spec", "text": "a" * 50}
    out = tools.read_link("https://wiki.example/page", lambda u: page, lambda u: db_mod.link_shared(conn, u), max_chars=10)
    assert out["title"] == "Spec" and out["text"] == "a" * 10
    assert out["truncated"] is True and out["chars"] == 50


@pytest.mark.unit
@pytest.mark.parametrize("final_url", [
    "https://id.atlassian.com/login?continue=x",
    "https://login.microsoftonline.com/common/oauth2",
    "https://lab.dev/users/sign_in",
])
def test_read_link_flags_login_walls(final_url):
    conn = _store_with_link()
    page = {"final_url": final_url, "title": "Log in", "text": "Sign in"}
    out = tools.read_link("https://wiki.example/page", lambda u: page, lambda u: db_mod.link_shared(conn, u))
    assert out["needs_login"] is True and "teams-reader open-link" in out["hint"]
    assert "text" not in out


@pytest.mark.unit
def test_read_link_explains_untrusted_certificates():
    conn = _store_with_link()

    def boom(_u):
        raise RuntimeError("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://wiki.example/page")

    out = tools.read_link("https://wiki.example/page", boom, lambda u: db_mod.link_shared(conn, u))
    assert "certificate" in out["error"]
