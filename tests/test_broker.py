import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from teams_reader.broker import client as client_mod
from teams_reader.broker import jobs as jobs_mod
from teams_reader.broker import server as server_mod
from teams_reader.browser.backfill import BackfillSummary
from teams_reader.store import db


class _FakeSession:
    template_url = "tpl"
    context = object()
    api_headers = {"authorization": "Bearer t"}

    def fetch(self, url):
        return {}


class _FakeLive:
    def __init__(self, db_file):
        self.db_file = db_file
        self.touches = 0
        self.interactive = []
        self.closed = False
        self.recovered = 0

    def touch(self):
        self.touches += 1

    def run(self, job, timeout=None, interactive=True):
        self.interactive.append(interactive)
        conn = db.connect(self.db_file)
        try:
            return job(_FakeSession(), conn)
        finally:
            conn.close()

    def status(self):
        return {"browser_started": True, "last_sync_age_s": 1, "last_error": None}

    def recover_login(self):
        self.recovered += 1

    def close(self):
        self.closed = True


@pytest.fixture
def live(tmp_path):
    db.connect(tmp_path / "s.db").close()
    return _FakeLive(tmp_path / "s.db")


@pytest.mark.unit
def test_job_runs_a_registered_job_on_the_live_browser(live, monkeypatch):
    monkeypatch.setitem(jobs_mod.JOBS, "echo", lambda session, conn, args: {"tpl": session.template_url, **args})
    broker = server_mod.Broker(live)
    reply = broker.handle({"op": "job", "job": "echo", "args": {"x": 1}, "interactive": False})
    assert reply == {"ok": {"tpl": "tpl", "x": 1}}
    assert live.interactive == [False]
    assert live.touches == 1


@pytest.mark.unit
def test_unknown_job_and_failures_come_back_as_errors(live, monkeypatch):
    broker = server_mod.Broker(live)
    assert broker.handle({"op": "job", "job": "rm -rf"})["error"] == "unknown job: rm -rf"

    def boom(session, conn, args):
        raise RuntimeError("VPN down")

    monkeypatch.setitem(jobs_mod.JOBS, "boom", boom)
    assert broker.handle({"op": "job", "job": "boom"}) == {"error": "VPN down", "type": "RuntimeError"}
    assert broker.handle({"op": "nope"})["error"] == "unknown op: nope"


@pytest.mark.unit
def test_fresh_touch_asks_the_live_sync_to_recover_a_lost_login(live):
    broker = server_mod.Broker(live)
    broker.handle({"op": "touch"})
    assert live.recovered == 0
    broker.handle({"op": "touch", "fresh": True})
    assert live.recovered == 1


@pytest.mark.unit
def test_broker_is_idle_only_after_no_request_for_idle_after(live):
    now = [100.0]
    broker = server_mod.Broker(live, idle_after=60, clock=lambda: now[0])
    assert not broker.idle()
    now[0] = 161.0
    assert broker.idle()
    broker.handle({"op": "status"})
    assert not broker.idle()


@pytest.mark.unit
def test_sync_job_returns_a_plain_summary(live, monkeypatch):
    seen = {}

    def fake_sync(conn, fetch, tpl, **kwargs):
        seen.update(kwargs)
        return BackfillSummary(chats_synced=2, new_messages=3, total_messages=9)

    monkeypatch.setattr(jobs_mod.backfill, "sync_with", fake_sync)
    reply = server_mod.Broker(live).handle(
        {"op": "job", "job": "sync", "args": {"chat_filter": "c1", "full": True, "max_pages": 5}}
    )
    assert reply == {"ok": {"chats_synced": 2, "new_messages": 3, "total_messages": 9,
                            "failed_chats": 0, "last_error": None}}
    assert seen == {"chat_filter": "c1", "full": True, "max_pages": 5}


@pytest.mark.unit
def test_read_link_job_refuses_links_nobody_shared(live):
    reply = server_mod.Broker(live).handle(
        {"op": "job", "job": "read_link", "args": {"url": "https://evil.example/"}}
    )
    assert reply == {"ok": {"error": "link was not shared in any chat"}}


@pytest.mark.unit
def test_transcripts_job_passes_the_session_auth_headers(live, monkeypatch):
    seen = {}

    def fake(conn, session, chat_id=None):
        seen["headers"] = session.api_headers
        seen["chat_id"] = chat_id
        return {"new": 0}

    monkeypatch.setattr(jobs_mod.transcripts, "sync_with", fake)
    server_mod.Broker(live).handle({"op": "job", "job": "transcripts", "args": {"chat_id": "19:x"}})
    assert seen == {"headers": {"authorization": "Bearer t"}, "chat_id": "19:x"}


@pytest.fixture
def short_dir():
    # pytest's tmp_path on macOS is longer than a Unix socket path may be.
    path = Path(tempfile.mkdtemp(prefix="trb", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def served(short_dir, live):
    sock = short_dir / "b.sock"
    broker = server_mod.Broker(live)
    srv = server_mod.listen(sock, broker)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield sock
    srv.shutdown()
    srv.server_close()


@pytest.mark.unit
def test_client_round_trip_over_the_socket(served, monkeypatch):
    monkeypatch.setitem(jobs_mod.JOBS, "echo", lambda session, conn, args: {"got": args})
    client = client_mod.BrokerClient(served, spawn=lambda: pytest.fail("must not spawn"))
    assert client.job("echo", x=[1, 2]) == {"got": {"x": [1, 2]}}
    assert client.status()["browser_started"] is True


@pytest.mark.unit
def test_client_raises_broker_errors(served):
    client = client_mod.BrokerClient(served, spawn=lambda: None)
    with pytest.raises(client_mod.BrokerError, match="unknown job: nope"):
        client.job("nope")


@pytest.mark.unit
def test_client_spawns_the_broker_when_none_is_running(short_dir, live):
    sock = short_dir / "b.sock"
    started = []

    def spawn():
        srv = server_mod.listen(sock, server_mod.Broker(live))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)

    client = client_mod.BrokerClient(sock, spawn=spawn, connect_timeout=5)
    try:
        assert client.status()["browser_started"] is True
        assert len(started) == 1
    finally:
        for srv in started:
            srv.shutdown()
            srv.server_close()


@pytest.mark.unit
def test_client_gives_up_when_the_broker_never_comes_up(short_dir):
    client = client_mod.BrokerClient(short_dir / "b.sock", spawn=lambda: None, connect_timeout=0.3)
    with pytest.raises(client_mod.BrokerError, match="did not start"):
        client.status()


@pytest.mark.unit
def test_socket_path_stays_under_the_unix_limit(short_dir):
    assert client_mod.socket_path(short_dir) == short_dir / "broker.sock"
    deep = short_dir / ("x" * 120)
    assert len(str(client_mod.socket_path(deep))) < 104


@pytest.mark.unit
def test_live_proxy_maps_sync_to_a_summary(served, monkeypatch):
    monkeypatch.setattr(
        jobs_mod.backfill, "sync_with",
        lambda conn, fetch, tpl, **kw: BackfillSummary(chats_synced=1, new_messages=0, total_messages=4),
    )
    proxy = client_mod.LiveProxy(client_mod.BrokerClient(served, spawn=lambda: None))
    summary = proxy.sync(chat_filter=None, full=False, max_pages=50)
    assert (summary.chats_synced, summary.new_messages, summary.total_messages) == (1, 0, 4)
    proxy.touch(fresh=True)
    proxy.close()  # a client never stops the shared broker
    assert proxy.status()["browser_started"] is True


@pytest.mark.unit
def test_a_running_job_keeps_the_broker_from_going_idle(live, monkeypatch):
    now = [0.0]
    broker = server_mod.Broker(live, idle_after=60, clock=lambda: now[0])

    def slow(session, conn, args):
        now[0] = 500.0
        assert not broker.idle()  # still busy at the idle check
        return "done"

    monkeypatch.setitem(jobs_mod.JOBS, "slow", slow)
    assert broker.handle({"op": "job", "job": "slow"}) == {"ok": "done"}
    assert not broker.idle()  # the clock restarts when the job ends
    now[0] = 561.0
    assert broker.idle()


@pytest.mark.unit
def test_a_closing_broker_refuses_new_work_without_running_it(live, monkeypatch):
    now = [0.0]
    broker = server_mod.Broker(live, idle_after=60, clock=lambda: now[0])
    ran = []
    monkeypatch.setitem(jobs_mod.JOBS, "echo", lambda s, c, a: ran.append(1))
    now[0] = 61.0
    assert broker.begin_shutdown_if_idle()
    reply = broker.handle({"op": "job", "job": "echo"})
    assert reply["type"] == "ShuttingDown"
    assert ran == []


@pytest.mark.unit
def test_client_retries_while_an_old_broker_shuts_down(short_dir, live, monkeypatch):
    sock = short_dir / "b.sock"
    closing = server_mod.Broker(live, idle_after=0)
    assert closing.begin_shutdown_if_idle()
    old = server_mod.listen(sock, closing)
    threading.Thread(target=old.serve_forever, daemon=True).start()
    spawns = []

    def spawn():
        spawns.append(1)
        if len(spawns) == 1:
            return
        old.shutdown()
        old.server_close()
        fresh = server_mod.listen(sock, server_mod.Broker(live))
        threading.Thread(target=fresh.serve_forever, daemon=True).start()
        spawned.append(fresh)

    spawned = []
    monkeypatch.setattr(client_mod, "_RESPAWN_EVERY_S", 0.05)
    client = client_mod.BrokerClient(sock, spawn=spawn, connect_timeout=5)
    try:
        assert client.status()["browser_started"] is True
    finally:
        for srv in spawned:
            srv.shutdown()
            srv.server_close()


@pytest.mark.unit
def test_client_error_keeps_the_remote_exception_type(served):
    with pytest.raises(client_mod.BrokerError) as info:
        client_mod.BrokerClient(served, spawn=lambda: None).job("nope")
    assert info.value.kind is None
    assert "unknown job" in str(info.value)


@pytest.mark.unit
def test_socket_is_private_to_the_user(served):
    import os
    import stat

    assert stat.S_IMODE(os.stat(served).st_mode) == 0o600
    assert client_mod.owned_by_me(served)


@pytest.mark.unit
def test_fresh_runs_a_sync_and_records_it(live, monkeypatch):
    from teams_reader.mcp import freshness

    synced = []
    monkeypatch.setattr(jobs_mod.backfill, "sync_with", lambda conn, fetch, tpl, **kw: synced.append(kw) or
                        BackfillSummary(chats_synced=1, new_messages=1, total_messages=1))
    now = [1000.0]
    broker = server_mod.Broker(live, wall=lambda: now[0])
    reply = broker.handle({"op": "fresh", "since": 999.0})
    assert reply["ok"]["synced"] is True
    assert len(synced) == 1
    conn = db.connect(live.db_file)
    assert freshness.synced_at(conn) == 1000.0


@pytest.mark.unit
def test_fresh_reuses_a_sync_that_started_after_the_question(live, monkeypatch):
    synced = []
    monkeypatch.setattr(jobs_mod.backfill, "sync_with", lambda conn, fetch, tpl, **kw: synced.append(1) or
                        BackfillSummary(chats_synced=0, new_messages=0, total_messages=0))
    now = [1000.0]
    broker = server_mod.Broker(live, wall=lambda: now[0])
    broker.handle({"op": "fresh", "since": 999.0})
    reply = broker.handle({"op": "fresh", "since": 999.5})  # asked before that sync began
    assert reply["ok"]["synced"] is False
    now[0] = 1010.0
    broker.handle({"op": "fresh", "since": 1009.0})  # a newer question needs a new sync
    assert len(synced) == 2


@pytest.mark.unit
def test_fresh_also_asks_the_live_sync_to_recover_a_lost_login(live, monkeypatch):
    monkeypatch.setattr(jobs_mod.backfill, "sync_with", lambda *a, **k:
                        BackfillSummary(chats_synced=0, new_messages=0, total_messages=0))
    broker = server_mod.Broker(live)
    broker.handle({"op": "fresh", "since": 0.0})
    assert live.recovered == 1 and live.touches >= 1


@pytest.mark.unit
def test_a_timed_out_fresh_sync_names_its_own_limit(live, monkeypatch):
    from concurrent.futures import TimeoutError as FutureTimeout

    def slow_run(job, timeout=None, interactive=True):
        raise FutureTimeout()

    monkeypatch.setattr(live, "run", slow_run)
    reply = server_mod.Broker(live).handle({"op": "fresh", "since": 0.0})
    assert reply["type"] == "TimeoutError"
    assert f"{server_mod.FRESH_TIMEOUT_S:.0f}s" in reply["error"]
    assert "still runs" in reply["error"]
