import pytest

from teams_reader import ingest as ingest_mod
from teams_reader.cache import accounts
from teams_reader.store import db as db_mod
from tests.fixtures.synthetic import (
    TENANT_A, TENANT_B, USER_A, USER_B, build_account_dump, database_name, message_value,
)

CHAT_A = "19:alpha@thread.v2"
CHAT_B = "19:beta@thread.v2"


def _two_accounts() -> str:
    return build_account_dump({
        (TENANT_A, USER_A): [message_value("a1", "Ann", "<p>from a</p>", 1_787_000_000_000, CHAT_A)],
        (TENANT_B, USER_B): [message_value("b1", "Ben", "<p>from b</p>", 1_787_000_100_000, CHAT_B)],
    })


@pytest.fixture()
def conn():
    connection = db_mod.connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture()
def dump(tmp_path):
    path = tmp_path / "d.jsonl"
    path.write_text(_two_accounts())
    return path


@pytest.mark.unit
def test_user_of_reads_the_standard_and_orgid_forms():
    assert accounts.user_of(database_name("replychain-manager", TENANT_A, USER_A)) == USER_A
    orgid = f"Teams:x:react-web-client:{TENANT_A}:8:orgid:{USER_A}:en-us"
    assert accounts.user_of(orgid) == USER_A
    assert accounts.user_of("teams-service-worker-v2") is None


@pytest.mark.unit
def test_message_accounts_lists_each_account_with_its_chats():
    from teams_reader.parse.decode import decode_stream

    found = accounts.message_accounts(list(decode_stream(_two_accounts())))
    assert {(a.user, a.tenant, a.chat_ids) for a in found} == {
        (USER_A, TENANT_A, frozenset({CHAT_A})),
        (USER_B, TENANT_B, frozenset({CHAT_B})),
    }


@pytest.mark.unit
def test_match_account_needs_a_unique_overlap():
    a = accounts.CacheAccount(USER_A, TENANT_A, frozenset({CHAT_A, "19:shared"}))
    b = accounts.CacheAccount(USER_B, TENANT_B, frozenset({CHAT_B, "19:shared"}))
    assert accounts.match_account([a, b], {CHAT_A, "19:x"}) == USER_A
    assert accounts.match_account([a, b], {"19:shared"}) is None
    assert accounts.match_account([a, b], set()) is None


@pytest.mark.unit
def test_mixed_cache_is_refused_until_the_account_is_known(conn, dump):
    with pytest.raises(accounts.MixedCacheError, match="2 Teams accounts") as info:
        ingest_mod.ingest_from_dump(conn, dump)
    assert USER_A in str(info.value) and "teams-reader login" in str(info.value)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


@pytest.mark.unit
def test_configured_account_keeps_only_its_messages(conn, dump):
    ingest_mod.ingest_from_dump(conn, dump, account=USER_B)
    rows = conn.execute("SELECT msg_id FROM messages").fetchall()
    assert [r["msg_id"] for r in rows] == ["b1"]


@pytest.mark.unit
def test_web_sync_picks_the_account_and_remembers_it(conn, dump):
    db_mod.set_web_chats(conn, [CHAT_A, "19:other"])
    ingest_mod.ingest_from_dump(conn, dump)
    assert [r["msg_id"] for r in conn.execute("SELECT msg_id FROM messages")] == ["a1"]
    assert db_mod.get_state(conn, accounts.ACCOUNT_STATE) == USER_A


@pytest.mark.unit
def test_web_sync_clears_a_pending_mixed_cache(conn, dump):
    db_mod.set_state(conn, "cache_fingerprint", "fp")
    with pytest.raises(accounts.MixedCacheError):
        ingest_mod.ingest_from_dump(conn, dump)
    db_mod.set_web_chats(conn, [CHAT_A])
    assert db_mod.get_state(conn, "cache_fingerprint") is None


@pytest.mark.unit
def test_single_account_cache_is_ingested_as_before(conn, tmp_path):
    path = tmp_path / "one.jsonl"
    path.write_text(build_account_dump({
        (TENANT_A, USER_A): [message_value("a1", "Ann", "<p>x</p>", 1_787_000_000_000, CHAT_A)],
    }))
    assert ingest_mod.ingest_from_dump(conn, path).total_messages == 1


@pytest.mark.unit
def test_list_chats_explains_an_unread_mixed_cache(conn, dump):
    from teams_reader.mcp import tools

    with pytest.raises(accounts.MixedCacheError):
        ingest_mod.ingest_from_dump(conn, dump)
    first = tools.list_chats(conn)[0]
    assert "Desktop cache not read" in first["notice"] and "teams-reader login" in first["notice"]

    db_mod.set_web_chats(conn, [CHAT_A])
    ingest_mod.ingest_from_dump(conn, dump)
    assert all("notice" not in row for row in tools.list_chats(conn))


@pytest.mark.unit
def test_database_ids_decoded_as_signed_are_mapped_back():
    assert accounts._unsigned(-102) == 154
    assert accounts._unsigned(-32768) == 32768
    assert accounts._unsigned(-129) == 65407
    assert accounts._unsigned(125) == 125


@pytest.mark.unit
def test_pinning_the_account_clears_the_notice(conn, dump):
    with pytest.raises(accounts.MixedCacheError):
        ingest_mod.ingest_from_dump(conn, dump)
    ingest_mod.ingest_from_dump(conn, dump, account=USER_A)
    assert db_mod.get_state(conn, accounts.PENDING_STATE) is None
