import json

import pytest

from teams_reader import ingest as ingest_mod
from teams_reader.store import db as db_mod
from tests.fixtures.synthetic import build_dump, message_value, sample_dump


@pytest.fixture()
def conn():
    connection = db_mod.connect(":memory:")
    yield connection
    connection.close()


def _dump_with_conversation() -> str:
    msgs = [
        message_value("m1", "Alice", "<p>hello</p>", 1_787_000_000_000),
        message_value("m2", "Bob", "<p>hi</p>", 1_787_000_100_000),
    ]
    conversation = {"id": "19:demo@thread.v2", "topic": "Demo Team"}
    return build_dump([*msgs, conversation])


@pytest.mark.unit
def test_ingest_from_dump_stores_messages_and_names(conn, tmp_path):
    dump = tmp_path / "d.jsonl"
    dump.write_text(_dump_with_conversation())
    result = ingest_mod.ingest_from_dump(conn, dump)

    assert result.new_messages == 2
    assert result.total_messages == 2
    assert result.chats_named == 1
    assert result.last_ts == 1_787_000_100_000

    chat = db_mod.list_chats(conn)[0]
    assert chat["display_name"] == "Demo Team"


@pytest.mark.unit
def test_ingest_from_dump_is_idempotent(conn, tmp_path):
    dump = tmp_path / "d.jsonl"
    dump.write_text(_dump_with_conversation())
    ingest_mod.ingest_from_dump(conn, dump)
    second = ingest_mod.ingest_from_dump(conn, dump)
    assert second.new_messages == 0
    assert second.total_messages == 2


@pytest.mark.unit
def test_ingest_from_empty_dump(conn, tmp_path):
    dump = tmp_path / "empty.jsonl"
    dump.write_text("")
    result = ingest_mod.ingest_from_dump(conn, dump)
    assert result.new_messages == 0
    assert result.total_messages == 0


@pytest.mark.unit
def test_ingest_from_sample_dump(conn, tmp_path):
    dump = tmp_path / "s.jsonl"
    dump.write_text(sample_dump())
    result = ingest_mod.ingest_from_dump(conn, dump)
    # m1, m2, m3 are messages; the control row c1 is skipped.
    assert result.new_messages == 3


@pytest.mark.unit
def test_ingest_full_pipeline_with_injected_steps(conn, tmp_path):
    fake_leveldb = tmp_path / "fake.leveldb"
    fake_leveldb.mkdir()

    def locator():
        return fake_leveldb

    def snapshot_fn(src, dest):
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def dump_fn(snap, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(_dump_with_conversation())
        return out

    result = ingest_mod.ingest(
        conn,
        locator=locator,
        work_dir=tmp_path / "work",
        snapshot_fn=snapshot_fn,
        dump_fn=dump_fn,
    )
    assert result.new_messages == 2


@pytest.mark.unit
def test_ingest_missing_leveldb_raises(conn):
    with pytest.raises(FileNotFoundError):
        ingest_mod.ingest(conn, locator=lambda: None)


@pytest.mark.unit
def test_incremental_pickup_of_new_message(conn, tmp_path):
    """A new message arriving in the cache between ticks is picked up, mirroring
    the daemon re-running ingest on a growing cache."""
    dump = tmp_path / "d.jsonl"
    first = build_dump([message_value("m1", "Alice", "<p>hello</p>", 1_787_000_000_000)])
    dump.write_text(first)
    assert ingest_mod.ingest_from_dump(conn, dump).new_messages == 1

    # Simulate a new message appearing in the cache, then the next tick.
    grown = first + "\n" + build_dump(
        [message_value("m2", "Bob", "<p>just arrived</p>", 1_787_000_900_000)]
    )
    dump.write_text(grown)
    result = ingest_mod.ingest_from_dump(conn, dump)
    assert result.new_messages == 1
    assert result.total_messages == 2


@pytest.mark.unit
def test_daemon_two_ticks_pick_up_growing_cache(conn, tmp_path):
    from teams_reader import daemon

    dump = tmp_path / "d.jsonl"
    dump.write_text(build_dump([message_value("m1", "Alice", "<p>a</p>", 1_787_000_000_000)]))
    counts = []

    def tick(_conn):
        return ingest_mod.ingest_from_dump(_conn, dump)

    # First tick ingests m1; between ticks a new message is written.
    def grow_then_record(result):
        counts.append(result.new_messages)
        if len(counts) == 1:
            dump.write_text(
                dump.read_text()
                + "\n"
                + build_dump([message_value("m2", "Bob", "<p>b</p>", 1_787_000_900_000)])
            )

    daemon.poll(conn, stop_after=2, ingest_fn=tick, sleep_fn=lambda _: None, on_result=grow_then_record)
    assert counts == [1, 1]
