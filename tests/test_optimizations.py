"""Latency and token optimisations of the MCP layer (2026-10-20 review)."""
import asyncio

import pytest

from teams_reader.mcp import freshness, livesync, tools
from teams_reader.parse.messages import Message
from teams_reader.store import db as db_mod


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture()
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "s.db")
    db_mod.upsert_messages(c, [
        Message("m1", "19:a@thread.v2", "Alice", 100, "deploying keycloak", None),
        Message("m2", "19:a@thread.v2", "Bob", 200, "ok", "deploying keycloak", edited=True),
        Message("m3", "19:b@thread.v2", "Carol", 300, "other", None),
    ])
    db_mod.set_chat_name(c, "19:a@thread.v2", "Project Sigma", "group")
    yield c
    c.close()


# --- 2. compact per-message payloads ----------------------------------------

@pytest.mark.unit
def test_read_chat_rows_omit_what_the_envelope_already_says(conn):
    out = tools.read_chat(conn, "Sigma")
    first, second = out["messages"]
    assert set(first) == {"msg_id", "sender", "time", "text"}
    assert second["quoted_text"] == "deploying keycloak" and second["edited"] is True
    assert out["last_ts"] == 200  # for paging with `since`


@pytest.mark.unit
def test_read_chat_last_ts_empty_chat(conn):
    assert tools.read_chat(conn, "Sigma", since=10_000)["last_ts"] is None


@pytest.mark.unit
def test_search_and_recent_rows_keep_the_chat(conn):
    hit = tools.search(conn, "keycloak")[0]
    assert {"chat_id", "chat_name", "ts", "time", "sender", "text", "msg_id"} <= set(hit)
    assert "quoted_text" not in hit and "edited" not in hit
    assert "chat_name" in tools.recent(conn)[0]


@pytest.mark.unit
def test_suggest_reply_rows_are_compact(conn):
    rows = tools.suggest_reply(conn, "Sigma")["recent"]
    assert "chat_id" not in rows[0] and "ts" not in rows[0]


# --- 3. tool annotations ------------------------------------------------------

@pytest.mark.integration
def test_read_only_tools_are_annotated(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setattr("teams_reader.refresh.maybe_refresh", lambda *a, **k: False)
    from teams_reader.mcp.server import build_server

    server = build_server(tmp_path / "store.db")
    by_name = {t.name: t for t in asyncio.run(server.list_tools())}
    for name, tool in by_name.items():
        assert tool.annotations is not None, name
        read_only = getattr(tool.annotations, "read_only_hint", None)  # mcp 2 field name
        if read_only is None:
            read_only = tool.annotations.readOnlyHint  # mcp 1
        assert read_only is (name != "sync_history"), name


# --- 4. only state-of-chat tools trigger and wait for a sync ------------------

@pytest.mark.integration
def test_only_fresh_tools_mark_use(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setattr("teams_reader.refresh.maybe_refresh", lambda *a, **k: False)
    from teams_reader.mcp.server import build_server

    db_file = tmp_path / "store.db"
    seed = db_mod.connect(db_file)
    db_mod.upsert_messages(seed, [Message("m1", "19:a@thread.v2", "Alice", 100, "hi", None)])
    seed.close()
    server = build_server(db_file)

    asyncio.run(server.call_tool("search", {"query": "hi"}))
    asyncio.run(server.call_tool("list_images", {"name_or_id": "19:a@thread.v2"}))
    check = db_mod.connect(db_file)
    assert freshness.used_at(check) is None
    asyncio.run(server.call_tool("read_chat", {"name_or_id": "19:a@thread.v2"}))
    assert freshness.used_at(check) is not None
    check.close()


# --- 5. background poller pacing ---------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("since_use, expected", [
    (0.0, 30.0), (livesync.ACTIVE_S - 1, 30.0), (livesync.ACTIVE_S + 1, 30.0 * livesync.QUIET_FACTOR),
])
def test_sync_interval_widens_once_the_conversation_goes_quiet(since_use, expected):
    assert livesync.sync_interval(30.0, since_use) == expected


@pytest.mark.unit
def test_poll_tick_slows_when_another_session_holds_the_browser():
    assert livesync.poll_tick(1.0, in_use_elsewhere=False) == 1.0
    assert livesync.poll_tick(1.0, in_use_elsewhere=True) == livesync.IN_USE_TICK_FACTOR


# --- 6. transcripts refresh is debounced -------------------------------------

@pytest.mark.integration
def test_list_transcripts_refreshes_at_most_once_per_window(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setattr("teams_reader.refresh.maybe_refresh", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(
        "teams_reader.browser.transcripts.sync_once", lambda conn, chat_id=None: calls.append(1) or {"new": 0}
    )
    from teams_reader.mcp.server import build_server

    server = build_server(tmp_path / "store.db")
    first = asyncio.run(server.call_tool("list_transcripts", {}))
    second = asyncio.run(server.call_tool("list_transcripts", {}))
    assert len(calls) == 1
    assert "skipped" in str(second) and "skipped" not in str(first)


# --- 7. ingest leaves no 50 MB dump behind -----------------------------------

@pytest.mark.unit
def test_ingest_cleans_its_work_files(tmp_path):
    from teams_reader import ingest as ingest_mod

    c = db_mod.connect(":memory:")
    leveldb = tmp_path / "leveldb"
    leveldb.mkdir()
    (leveldb / "000001.ldb").write_bytes(b"x")

    def fake_dump(snap, out):
        out.write_text("", encoding="utf-8")
        return out

    work = tmp_path / "work"
    ingest_mod.ingest(c, locator=lambda: leveldb, work_dir=work, dump_fn=fake_dump)
    assert not (work / "dump.jsonl").exists()
    assert not (work / "snapshot").exists()
    c.close()


# --- 8. browser disk cache is capped ------------------------------------------

@pytest.mark.unit
def test_launch_options_cap_the_disk_cache():
    from teams_reader.browser import session

    options = session.launch_options(headless=True)
    assert options["headless"] is True
    assert any(a.startswith("--disk-cache-size=") for a in options["args"])
