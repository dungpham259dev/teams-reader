import pytest

from teams_reader.daemon import poll
from teams_reader.ingest import IngestResult


def _ok(_conn):
    return IngestResult(new_messages=1, total_messages=1, chats_named=0, last_ts=1)


@pytest.mark.unit
def test_poll_runs_fixed_iterations_without_real_sleep():
    sleeps: list[float] = []
    results: list[IngestResult] = []
    ran = poll(
        conn=None,
        interval=9.0,
        stop_after=3,
        ingest_fn=_ok,
        sleep_fn=sleeps.append,
        on_result=results.append,
    )
    assert ran == 3
    assert len(results) == 3
    # No sleep after the final iteration.
    assert sleeps == [9.0, 9.0]


@pytest.mark.unit
def test_poll_survives_failing_tick():
    calls = {"n": 0}

    def flaky(_conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return IngestResult(1, 1, 0, 1)

    seen: list[IngestResult] = []
    ran = poll(None, stop_after=2, ingest_fn=flaky, sleep_fn=lambda _: None, on_result=seen.append)
    assert ran == 2
    assert seen[0].total_messages == -1   # failed tick marker
    assert seen[1].total_messages == 1


@pytest.mark.unit
def test_poll_zero_iterations():
    ran = poll(None, stop_after=0, ingest_fn=_ok, sleep_fn=lambda _: None)
    assert ran == 0


@pytest.mark.unit
def test_wait_for_new_message_detects_on_second_poll():
    from teams_reader.daemon import wait_for_new_message

    # baseline call, then first poll 0 new, second poll 1 new.
    seq = iter([
        IngestResult(0, 10, 0, 1),  # baseline
        IngestResult(0, 10, 0, 1),  # poll 1: nothing
        IngestResult(1, 11, 0, 2),  # poll 2: new message
    ])
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    ok = wait_for_new_message(
        conn=None,
        timeout=100,
        interval=1,
        ingest_fn=lambda _c: next(seq),
        sleep_fn=lambda _s: None,
        now_fn=lambda: next(clock),
    )
    assert ok is True


@pytest.mark.unit
def test_wait_for_new_message_times_out():
    from teams_reader.daemon import wait_for_new_message

    clock = iter([0.0, 5.0, 10.0, 15.0])  # exceeds timeout quickly
    ok = wait_for_new_message(
        conn=None,
        timeout=8,
        interval=1,
        ingest_fn=lambda _c: IngestResult(0, 10, 0, 1),
        sleep_fn=lambda _s: None,
        now_fn=lambda: next(clock),
    )
    assert ok is False
