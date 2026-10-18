"""Query the MCP server over the real stdio protocol: spawn the installed
`teams-reader mcp` process against a seeded (synthetic) store and call tools
through an MCP client. Verifies the end-to-end MCP path, not just build_server.

Skipped unless mcp and the installed console script are available.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from teams_reader.parse.messages import Message
from teams_reader.store import db as db_mod

_CLI = Path(sys.executable).with_name("teams-reader")


async def _query(db_file: Path):
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    env = dict(os.environ)
    env["TEAMS_READER_DB"] = str(db_file)
    env["TEAMS_READER_NO_REFRESH"] = "1"  # don't touch the real cache in tests
    params = StdioServerParameters(command=str(_CLI), args=["mcp"], env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            search = await session.call_tool("search", {"query": "keycloak"})
            chats = await session.call_tool("list_chats", {})
            return tools, str(search), str(chats)


@pytest.mark.integration
def test_query_server_over_stdio(tmp_path):
    pytest.importorskip("mcp")
    if not _CLI.exists():
        pytest.skip("teams-reader console script not installed (pip install -e .)")

    db_file = tmp_path / "store.db"
    seed = db_mod.connect(db_file)
    db_mod.upsert_messages(
        seed,
        [Message("m1", "19:a@thread.v2", "Alice", 100, "deploying keycloak to prod", None)],
    )
    db_mod.set_chat_name(seed, "19:a@thread.v2", "Project Sigma", "group")
    seed.close()

    tools, search_out, chats_out = asyncio.run(asyncio.wait_for(_query(db_file), timeout=30))

    assert {"list_chats", "read_chat", "search", "recent", "ask"} <= tools
    assert "keycloak" in search_out
    assert "Project Sigma" in chats_out
