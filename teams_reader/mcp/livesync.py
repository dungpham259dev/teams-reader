"""Background sync for the MCP server over one warm browser.

The browser starts on the first tool call (not at server start), then the
server's chats are re-synced every ``interval`` seconds while the tools stay in
use, so a just-sent message shows up without an explicit ``sync_history``.
Tool calls never wait on it. Disable per profile with ``live_sync = false``.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from teams_reader import logs
from teams_reader.browser.backfill import sync_with
from teams_reader.browser.live import LiveBrowser, PlaywrightSession, should_sync
from teams_reader.mcp import freshness
from teams_reader.store import db as db_mod

DEFAULT_INTERVAL = 30.0
DEFAULT_IDLE_AFTER = 900.0
_BACKOFF_CAP = 600.0
_BACKGROUND_MAX_PAGES = 20
_MIN_GAP = 3.0  # between syncs triggered by questions
ACTIVE_S = 120.0  # after the last question, keep the short interval this long
QUIET_FACTOR = 4
IN_USE_TICK_FACTOR = 5
_log = logs.get("sync")


def backoff(interval: float, failures: int, cap: float = _BACKOFF_CAP) -> float:
    """Wait longer after consecutive failures (VPN down, not logged in)."""
    return min(interval * (2 ** min(failures, 10)), cap)


def sync_interval(interval: float, since_use: float) -> float:
    """Short interval right after a question, wider once the conversation is quiet."""
    return interval if since_use <= ACTIVE_S else interval * QUIET_FACTOR


def poll_tick(tick: float, in_use_elsewhere: bool) -> float:
    """Poll slower when another session's browser does the syncing."""
    return tick * IN_USE_TICK_FACTOR if in_use_elsewhere else tick


def enabled() -> bool:
    from teams_reader import settings

    return settings.active().live_sync


class LiveSync:
    def __init__(
        self,
        db_file: Path,
        browser_factory: Callable[[], Any] = lambda: LiveBrowser(
            lambda interactive: PlaywrightSession(interactive=interactive)
        ),
        interval: float = DEFAULT_INTERVAL,
        idle_after: float = DEFAULT_IDLE_AFTER,
        tick: float = 1.0,
    ) -> None:
        self._db_file = db_file
        self._factory = browser_factory
        self._interval = interval
        self._idle_after = idle_after
        self._tick = tick
        self._browser: Any = None
        self._conn: sqlite3.Connection | None = None
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None
        self._last_use: float | None = None
        self._last_sync: float | None = None
        self._last_error: str | None = None
        self._failures = 0
        self._synced_wall: float | None = None
        self._in_use_elsewhere = False

    def touch(self) -> None:
        """Note a tool call; starts the background poller on first use."""
        self._last_use = time.monotonic()
        with self._guard:
            if self._poller is None:
                self._poller = threading.Thread(target=self._poll, name="teams-live-sync", daemon=True)
                self._poller.start()

    def run(
        self,
        job: Callable[[Any, sqlite3.Connection], Any],
        timeout: float | None = None,
        interactive: bool = True,
    ) -> Any:
        """Run ``job(session, conn)`` on the browser thread with its own connection.

        Tool calls are user-initiated (``interactive``) and may sign in; the
        background poller passes False so it never triggers MFA prompts.
        """
        browser = self._ensure_browser()
        return browser.run(
            lambda session: job(session, self._worker_conn()), timeout=timeout, interactive=interactive
        )

    def sync(self, interactive: bool = True, **kwargs: Any) -> Any:
        return self.run(
            lambda s, conn: sync_with(conn, s.fetch, s.template_url, **kwargs), interactive=interactive
        )

    def status(self) -> dict[str, Any]:
        age = None if self._last_sync is None else round(time.monotonic() - self._last_sync)
        return {"browser_started": self._browser is not None, "last_sync_age_s": age, "last_error": self._last_error}

    def close(self) -> None:
        self._stop.set()
        if self._poller is not None:
            self._poller.join(timeout=10)
        if self._browser is not None:
            self._browser.close()

    def _ensure_browser(self) -> Any:
        with self._guard:
            if self._browser is None:
                _log.info("starting the warm browser")
                self._browser = self._factory()
            return self._browser

    def _worker_conn(self) -> sqlite3.Connection:
        # Only ever used on the single browser thread.
        if self._conn is None:
            self._conn = db_mod.connect(self._db_file)
        return self._conn

    def _poll(self) -> None:
        conn = db_mod.connect(self._db_file)  # this thread's own connection
        try:
            while not self._stop.wait(poll_tick(self._tick, self._in_use_elsewhere)):
                self._poll_once(conn)
        finally:
            conn.close()

    def _due(self, conn: sqlite3.Connection, now: float) -> bool:
        """Sync on the interval while any session uses the tools, and at once
        after a question newer than the last sync (from any session)."""
        shared = freshness.used_at(conn)
        last_use = self._last_use
        if shared is not None:
            shared_mono = now - (time.time() - shared)
            last_use = shared_mono if last_use is None else max(last_use, shared_mono)
        if self._failures:
            interval = backoff(self._interval, self._failures)
            return should_sync(now, last_use, self._last_sync, interval, self._idle_after)
        asked = shared is not None and (self._synced_wall is None or shared > self._synced_wall)
        gap_ok = self._last_sync is None or now - self._last_sync >= _MIN_GAP
        interval = sync_interval(self._interval, now - last_use if last_use is not None else 0.0)
        return should_sync(now, last_use, self._last_sync, interval, self._idle_after) or (asked and gap_ok)

    def _poll_once(self, conn: sqlite3.Connection) -> None:
        from teams_reader.browser.profile_lock import ProfileInUse

        now = time.monotonic()
        if not self._due(conn, now):
            return
        started_wall = time.time()
        try:
            summary = self.sync(interactive=False, max_pages=_BACKGROUND_MAX_PAGES)
            _log.info("background sync: %s chats changed, %s new messages (%.1fs)",
                      getattr(summary, "chats_synced", "?"), getattr(summary, "new_messages", "?"),
                      time.monotonic() - now)
            self._last_error = None
            self._failures = 0
            self._in_use_elsewhere = False
            self._synced_wall = started_wall
            freshness.mark_synced(conn, started_wall)
        except ProfileInUse as exc:
            self._last_error = str(exc)[:300]
            self._failures += 1
            self._in_use_elsewhere = True
            _log.info("another session holds the browser and syncs for everyone (%s)", self._last_error[:80])
        except Exception as exc:  # noqa: BLE001 - reported via status(), retried later
            self._last_error = str(exc)[:300]
            self._failures += 1
            freshness.mark_failed(conn, self._last_error)
            _log.warning("background sync failed (%d in a row; next try in %ds): %s", self._failures,
                         backoff(self._interval, self._failures), self._last_error)
        self._last_sync = time.monotonic()
