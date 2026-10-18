"""Background poll loop: re-run incremental ingest on an interval.

Reads only the local cache, so each tick is cheap and offline. The loop is
parameterised (iteration cap, sleep and ingest functions injectable) so it can
be tested without real time or a real cache.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Callable

from teams_reader.ingest import IngestResult, ingest

DEFAULT_INTERVAL = 45.0

IngestFn = Callable[[sqlite3.Connection], IngestResult]
ResultHook = Callable[[IngestResult], None]


def poll(
    conn: sqlite3.Connection,
    interval: float = DEFAULT_INTERVAL,
    stop_after: int | None = None,
    ingest_fn: IngestFn = ingest,
    sleep_fn: Callable[[float], None] = time.sleep,
    on_result: ResultHook | None = None,
) -> int:
    """Run ingest every ``interval`` seconds.

    ``stop_after`` caps the number of iterations (``None`` = run forever).
    Returns the number of iterations completed. A single failing tick is logged
    through ``on_result`` is skipped, never crashing the loop.
    """
    iterations = 0
    while stop_after is None or iterations < stop_after:
        try:
            result = ingest_fn(conn)
            if on_result is not None:
                on_result(result)
        except Exception as error:  # noqa: BLE001 - a bad tick must not kill the loop
            if on_result is not None:
                on_result(_error_result(error))
        iterations += 1
        if stop_after is not None and iterations >= stop_after:
            break
        sleep_fn(interval)
    return iterations


def _error_result(error: Exception) -> IngestResult:
    # Surfaced through on_result for logging; zero counts signal a skipped tick.
    return IngestResult(new_messages=0, total_messages=-1, chats_named=0, last_ts=0)


def wait_for_new_message(
    conn: sqlite3.Connection,
    timeout: float = 120.0,
    interval: float = 5.0,
    ingest_fn: IngestFn = ingest,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll the cache until a new message appears or ``timeout`` elapses.

    Used by the live check: run it, send yourself a Teams message, and it returns
    True as soon as that message reaches the local cache.
    """
    deadline = now_fn() + timeout
    ingest_fn(conn)  # baseline the current cache
    while now_fn() < deadline:
        sleep_fn(interval)
        if ingest_fn(conn).new_messages > 0:
            return True
    return False
