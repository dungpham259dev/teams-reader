import pytest

from teams_reader import setup_steps
from teams_reader.browser.backfill import BackfillSummary
from teams_reader.browser.profile_lock import ProfileInUse
from teams_reader.ingest import IngestResult


@pytest.mark.unit
def test_read_cache_reports_counts_and_missing_cache():
    ok = setup_steps.read_cache("default", ingest_fn=lambda conn: IngestResult(10, 586, 2, 1))
    assert ok.ok and "586 messages" in ok.detail

    def missing(conn):
        raise FileNotFoundError("No Teams message cache for this profile")

    bad = setup_steps.read_cache("default", ingest_fn=missing)
    assert not bad.ok and "No Teams message cache" in bad.detail


@pytest.mark.unit
def test_connect_reports_sync_or_explains_how_to_retry():
    ok = setup_steps.connect("default", backfill_fn=lambda conn, **kw: BackfillSummary(15, 666, 666))
    assert ok.ok and "15 chats" in ok.detail and "666 messages" in ok.detail

    def busy(conn, **kw):
        raise ProfileInUse("in use")

    held = setup_steps.connect("default", backfill_fn=busy)
    assert not held.ok and "MCP server" in held.detail and "teams-reader --profile default login" in held.detail

    def vpn(conn, **kw):
        raise RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED")

    down = setup_steps.connect("default", backfill_fn=vpn)
    assert not down.ok and "ERR_SOCKS" in down.detail and "teams-reader --profile default login" in down.detail


@pytest.mark.unit
def test_connect_asks_for_a_visible_full_sync():
    seen = {}

    def capture(conn, **kw):
        seen.update(kw)
        return BackfillSummary(1, 1, 1)

    setup_steps.connect("default", backfill_fn=capture)
    assert seen["headless"] is False and seen["full"] is True


@pytest.mark.unit
def test_read_cache_explains_a_shared_cache():
    from teams_reader.cache.accounts import MixedCacheError

    def mixed(conn):
        raise MixedCacheError("2 accounts")

    result = setup_steps.read_cache("default", ingest_fn=mixed)
    assert not result.ok and "after the Teams web sign-in" in result.detail


@pytest.mark.unit
def test_connect_rereads_a_cache_waiting_on_the_web_chats():
    from teams_reader.cache.accounts import PENDING_STATE
    from teams_reader.store import db as db_mod

    calls = []

    def backfill(conn, **kw):
        db_mod.set_state(conn, PENDING_STATE, "waiting")
        return BackfillSummary(1, 1, 1)

    def ingest(conn):
        calls.append(1)
        db_mod.delete_state(conn, PENDING_STATE)

    result = setup_steps.connect("default", backfill_fn=backfill, ingest_fn=ingest)
    assert calls == [1] and "desktop cache matched" in result.detail
