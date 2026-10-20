import pytest

from teams_reader.mcp import freshness
from teams_reader.store import db as db_mod


@pytest.fixture()
def conn(tmp_path):
    connection = db_mod.connect(tmp_path / "s.db")
    yield connection
    connection.close()


@pytest.mark.unit
def test_report_says_how_old_the_web_data_is(conn):
    assert freshness.report(conn, now=1_000.0)["web_sync"] == "never"
    freshness.mark_synced(conn, 970.0)
    report = freshness.report(conn, now=1_000.0)
    assert report["web_sync_age_s"] == 30 and "as_of" in report
    freshness.mark_failed(conn, "Teams session expired; run `teams-reader login`")
    assert "session expired" in freshness.report(conn, now=1_000.0)["web_sync_error"]


@pytest.mark.unit
def test_use_is_shared_through_the_store(conn):
    assert freshness.used_at(conn) is None
    freshness.mark_used(conn, 1_234.5)
    assert freshness.used_at(conn) == 1_234.5
