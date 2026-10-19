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
from teams_reader.store import db as db_mod

DEFAULT_INTERVAL = 30.0
DEFAULT_IDLE_AFTER = 900.0
_BACKOFF_CAP = 600.0
_BACKGROUND_MAX_PAGES = 20
_log = logs.get("sync")


def backoff(interval: float, failures: int, cap: float = _BACKOFF_CAP) -> float:
    """Wait longer after consecutive failures (VPN down, not logged in)."""
    return min(interval * (2 ** min(failures, 10)), cap)


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
        tick: float = 5.0,
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
        while not self._stop.wait(self._tick):
            now = time.monotonic()
            interval = backoff(self._interval, self._failures) if self._failures else self._interval
            if not should_sync(now, self._last_use, self._last_sync, interval, self._idle_after):
                continue
            try:
                summary = self.sync(interactive=False, max_pages=_BACKGROUND_MAX_PAGES)
                _log.info("background sync: %s chats changed, %s new messages (%.1fs)",
                          getattr(summary, "chats_synced", "?"), getattr(summary, "new_messages", "?"),
                          time.monotonic() - now)
                self._last_error = None
                self._failures = 0
            except Exception as exc:  # noqa: BLE001 - reported via status(), retried later
                self._last_error = str(exc)[:300]
                self._failures += 1
                _log.warning("background sync failed (%d in a row; next try in %ds): %s", self._failures,
                             backoff(self._interval, self._failures), self._last_error)
            self._last_sync = time.monotonic()
