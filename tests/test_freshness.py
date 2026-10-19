import pytest

from teams_reader.mcp import freshness
from teams_reader.store import db as db_mod


@pytest.fixture()
def conn(tmp_path):
    connection = db_mod.connect(tmp_path / "s.db")
    yield connection
    connection.close()


class Clock:
    def __init__(self, t=1_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.mark.unit
def test_recent_sync_is_fresh_without_waiting(conn):
    freshness.mark_synced(conn, 990.0)
    clock = Clock(1_000.0)
    assert freshness.wait_fresh(conn, used_at=1_000.0, syncing=True, now=clock, sleep=clock.sleep) == "fresh"
    assert clock.t == 1_000.0


@pytest.mark.unit
def test_waits_for_the_sync_a_question_triggers(conn):
    freshness.mark_synced(conn, 900.0)
    clock = Clock(1_000.0)

    def sleep(s):
        clock.sleep(s)
        if clock.t >= 1_002.0:
            freshness.mark_synced(conn, clock.t)

    assert freshness.wait_fresh(conn, used_at=1_000.0, syncing=True, now=clock, sleep=sleep) == "synced"
    assert clock.t < 1_004.0


@pytest.mark.unit
def test_gives_up_after_the_wait_budget(conn):
    freshness.mark_synced(conn, 900.0)
    clock = Clock(1_000.0)
    assert freshness.wait_fresh(conn, used_at=1_000.0, syncing=True, now=clock, sleep=clock.sleep) == "stale"
    assert clock.t <= 1_000.0 + freshness.WAIT_S + 1


@pytest.mark.unit
def test_never_waits_when_nothing_is_syncing(conn):
    clock = Clock()
    assert freshness.wait_fresh(conn, used_at=1_000.0, syncing=False, now=clock, sleep=clock.sleep) == "not syncing"
    freshness.mark_synced(conn, 100.0)
    freshness.mark_failed(conn, "login required")
    assert freshness.wait_fresh(conn, used_at=1_000.0, syncing=True, now=clock, sleep=clock.sleep) == "not syncing"
    assert clock.t == 1_000.0


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
