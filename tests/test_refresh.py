import pytest

from teams_reader import refresh


@pytest.fixture(autouse=True)
def _reset():
    refresh.reset()
    yield
    refresh.reset()


@pytest.mark.unit
def test_first_call_refreshes_then_debounces():
    calls = []
    clock = iter([100.0, 105.0, 120.0])
    ran1 = refresh.maybe_refresh(None, min_interval=10, now_fn=lambda: next(clock),
                                 ingest_fn=lambda c: calls.append(1))
    ran2 = refresh.maybe_refresh(None, min_interval=10, now_fn=lambda: next(clock),
                                 ingest_fn=lambda c: calls.append(1))  # +5s -> debounced
    ran3 = refresh.maybe_refresh(None, min_interval=10, now_fn=lambda: next(clock),
                                 ingest_fn=lambda c: calls.append(1))  # +20s -> runs
    assert ran1 is True
    assert ran2 is False
    assert ran3 is True
    assert len(calls) == 2


@pytest.mark.unit
def test_env_var_disables_refresh(monkeypatch):
    monkeypatch.setenv("TEAMS_READER_NO_REFRESH", "1")
    calls = []
    ran = refresh.maybe_refresh(None, min_interval=0, now_fn=lambda: 1.0,
                                ingest_fn=lambda c: calls.append(1))
    assert ran is False
    assert calls == []


@pytest.mark.unit
def test_refresh_swallows_errors():
    def boom(_c):
        raise RuntimeError("cache locked")

    ran = refresh.maybe_refresh(None, min_interval=0, now_fn=lambda: 1.0, ingest_fn=boom)
    assert ran is False


@pytest.mark.unit
def test_unchanged_fingerprint_skips_ingest():
    calls = []
    clock = iter([100.0, 200.0, 300.0])
    prints = iter(["A", "A", "B"])
    kwargs = dict(min_interval=10, now_fn=lambda: next(clock),
                  ingest_fn=lambda c: calls.append(1), fingerprint_fn=lambda: next(prints))
    assert refresh.maybe_refresh(None, **kwargs) is True
    assert refresh.maybe_refresh(None, **kwargs) is False  # cache untouched -> no 5s dump
    assert refresh.maybe_refresh(None, **kwargs) is True
    assert len(calls) == 2


@pytest.mark.unit
def test_failed_ingest_does_not_record_fingerprint():
    outcomes = iter([RuntimeError("locked"), None])

    def flaky(_c):
        err = next(outcomes)
        if err:
            raise err

    clock = iter([100.0, 200.0])
    kwargs = dict(min_interval=10, now_fn=lambda: next(clock), ingest_fn=flaky,
                  fingerprint_fn=lambda: "A")
    assert refresh.maybe_refresh(None, **kwargs) is False
    assert refresh.maybe_refresh(None, **kwargs) is True


@pytest.mark.unit
def test_leveldb_fingerprint_tracks_ldb_and_manifest(tmp_path):
    (tmp_path / "000001.ldb").write_bytes(b"a")
    (tmp_path / "MANIFEST-000001").write_bytes(b"m")
    (tmp_path / "000002.log").write_bytes(b"l")
    first = refresh.leveldb_fingerprint(tmp_path)
    (tmp_path / "000002.log").write_bytes(b"longer log")
    assert refresh.leveldb_fingerprint(tmp_path) == first  # dfindexeddb ignores .log
    (tmp_path / "000003.ldb").write_bytes(b"new")
    assert refresh.leveldb_fingerprint(tmp_path) != first
    assert refresh.leveldb_fingerprint(None) is None


@pytest.mark.unit
def test_work_cache_fingerprint_locates_once(tmp_path):
    (tmp_path / "000001.ldb").write_bytes(b"a")
    calls = []

    def locator():
        calls.append(1)
        return tmp_path

    first = refresh.work_cache_fingerprint(locator)
    assert refresh.work_cache_fingerprint(locator) == first
    assert len(calls) == 1


@pytest.mark.unit
def test_fingerprint_survives_restart_via_store():
    from teams_reader.store import db as db_mod

    conn = db_mod.connect(":memory:")
    calls = []
    kwargs = dict(min_interval=0, now_fn=lambda: 1.0,
                  ingest_fn=lambda c: calls.append(1), fingerprint_fn=lambda: "A")
    assert refresh.maybe_refresh(conn, **kwargs) is True
    refresh.reset()  # simulates a new server process
    assert refresh.maybe_refresh(conn, **kwargs) is False
    assert len(calls) == 1


@pytest.mark.unit
def test_fingerprint_includes_parser_version(tmp_path, monkeypatch):
    (tmp_path / "000001.ldb").write_bytes(b"a")
    first = refresh.leveldb_fingerprint(tmp_path)
    monkeypatch.setattr(refresh, "PARSER_VERSION", refresh.PARSER_VERSION + 1)
    assert refresh.leveldb_fingerprint(tmp_path) != first


@pytest.mark.unit
def test_cleared_store_forgets_fingerprint():
    from teams_reader.store import db as db_mod

    conn = db_mod.connect(":memory:")
    calls = []
    kwargs = dict(min_interval=0, now_fn=lambda: 1.0,
                  ingest_fn=lambda c: calls.append(1), fingerprint_fn=lambda: "A")
    refresh.maybe_refresh(conn, **kwargs)
    db_mod.clear_all(conn)
    assert refresh.maybe_refresh(conn, **kwargs) is True
    assert len(calls) == 2
