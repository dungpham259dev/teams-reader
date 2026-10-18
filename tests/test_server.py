import asyncio

import pytest

from teams_reader.parse.messages import Message
from teams_reader.store import db as db_mod


@pytest.mark.integration
def test_build_server_registers_and_runs_tools(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    # Don't touch the real Teams cache during the test's auto-refresh hook.
    monkeypatch.setattr("teams_reader.refresh.maybe_refresh", lambda *a, **k: False)
    from teams_reader.mcp.server import build_server

    db_file = tmp_path / "store.db"
    seed = db_mod.connect(db_file)
    db_mod.upsert_messages(
        seed,
        [Message("m1", "19:a@thread.v2", "Alice", 100, "deploying keycloak", None)],
    )
    db_mod.set_chat_name(seed, "19:a@thread.v2", "Project Sigma", "group")
    seed.close()

    server = build_server(db_file)

    tool_names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"list_chats", "read_chat", "search", "recent", "ask"} <= tool_names

    result = asyncio.run(server.call_tool("search", {"query": "keycloak"}))
    assert "keycloak" in str(result)

    chats = asyncio.run(server.call_tool("list_chats", {}))
    assert "Project Sigma" in str(chats)

    read = asyncio.run(server.call_tool("read_chat", {"name_or_id": "Sigma"}))
    assert "m1" in str(read)

    recent = asyncio.run(server.call_tool("recent", {"since": 0}))
    assert "m1" in str(recent)

    ask = asyncio.run(server.call_tool("ask", {"question": "keycloak deploy"}))
    assert "keycloak" in str(ask)


@pytest.mark.integration
def test_server_class_resolves():
    pytest.importorskip("mcp")
    from teams_reader.mcp.server import _server_class

    assert _server_class() is not None


@pytest.mark.integration
def test_read_image_returns_inline_and_deletes_file(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    monkeypatch.setattr("teams_reader.refresh.maybe_refresh", lambda *a, **k: False)
    from teams_reader.mcp.server import build_server

    url = "https://h/v1/objects/O/views/imgo"
    img = tmp_path / "pic.jpg"
    img.write_bytes(b"\xff\xd8fakejpeg")
    db_file = tmp_path / "store.db"
    seed = db_mod.connect(db_file)
    db_mod.upsert_messages(
        seed, [Message("mi", "19:a@thread.v2", "Alice", 1, "pic", None, image_urls=(url,))]
    )
    db_mod.set_image_path(seed, "mi", url, str(img))
    seed.close()

    result = asyncio.run(build_server(db_file).call_tool("read_image", {"msg_id": "mi"}))

    assert "image" in str(result).lower()
    assert not img.exists()
