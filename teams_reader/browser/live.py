"""A long-lived ("warm") Teams browser owned by one worker thread.

Playwright's sync API is bound to the thread that started it, and the reader's
persistent profile can only be open once, so every browser job (sync, image
download, link read) is queued to this single worker. The session opens lazily
on the first job, is reused afterwards, and is reopened once when the captured
auth stops working (401/403).
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable, Protocol

_AUTH_ERRORS = {401, 403}


class Session(Protocol):
    def fetch(self, url: str) -> object: ...

    def close(self) -> None: ...


class AuthExpired(RuntimeError):
    """The captured Teams auth was rejected; the session must be reopened."""


class LoginRequired(RuntimeError):
    """The Teams session needs a sign-in, which background work never attempts."""


def checked(payload: object) -> object:
    """Raise :class:`AuthExpired` for an auth-error payload from ``fetch``."""
    if isinstance(payload, dict) and payload.get("error") in _AUTH_ERRORS:
        raise AuthExpired(f"chatsvc returned {payload['error']}")
    return payload


def should_sync(
    now: float,
    last_use: float | None,
    last_sync: float | None,
    interval: float,
    idle_after: float,
) -> bool:
    """Background-sync only while the tools are in use, at most every ``interval``."""
    if last_use is None or now - last_use > idle_after:
        return False
    return last_sync is None or now - last_sync >= interval


def _log():
    from teams_reader import logs

    return logs.get("browser")


class LiveBrowser:
    def __init__(self, opener: Callable[[bool], Session]) -> None:
        self._opener = opener
        self._session: Session | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._closed = False
        self._state = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="teams-live-browser", daemon=True)
        self._thread.start()

    def run(
        self, job: Callable[[Session], Any], timeout: float | None = None, interactive: bool = False
    ) -> Any:
        """Run ``job(session)`` on the browser thread and return its result.

        Only ``interactive`` (user-initiated) jobs may sign in when the session
        has expired; background jobs fail with :class:`LoginRequired` instead.
        """
        future: Future = Future()
        with self._state:
            if self._closed:
                raise RuntimeError("live browser is closed")
            self._jobs.put((job, interactive, future))
        return future.result(timeout=timeout)

    def close(self) -> None:
        with self._state:
            if self._closed:
                return
            self._closed = True
            self._jobs.put(None)
        self._thread.join(timeout=30)

    def _loop(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                self._drop_session()
                self._fail_pending()
                return
            job, interactive, future = item
            try:
                future.set_result(self._run_with_retry(job, interactive))
            except BaseException as exc:  # noqa: BLE001 - delivered to the caller
                future.set_exception(exc)

    def _fail_pending(self) -> None:
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                item[2].set_exception(RuntimeError("live browser is closed"))

    def _run_with_retry(self, job: Callable[[Session], Any], interactive: bool) -> Any:
        try:
            return job(self._ensure(interactive))
        except AuthExpired:
            _log().info("Teams session expired; reopening the browser")
            self._drop_session()
            return job(self._ensure(interactive))
        except Exception:
            if not self._alive():
                _log().warning("the browser died; it reopens on the next job")
                self._drop_session()  # crashed browser: reopen on the next job
            raise

    def _alive(self) -> bool:
        probe = getattr(self._session, "alive", None)
        if probe is None:
            return True
        try:
            return bool(probe())
        except Exception:  # noqa: BLE001 - a failing probe means a dead session
            return False

    def _ensure(self, interactive: bool) -> Session:
        if self._session is None:
            started = time.monotonic()
            _log().info("opening the reader browser (%s)", "may sign in" if interactive else "background, never signs in")
            try:
                self._session = self._opener(interactive)  # failures propagate; retried next job
            except Exception as exc:
                _log().warning("browser could not open after %.0fs: %s", time.monotonic() - started, exc)
                raise
            _log().info("browser ready in %.0fs; Teams API captured, page parked on about:blank",
                        time.monotonic() - started)
        return self._session

    def _drop_session(self) -> None:
        if self._session is not None:
            _log().info("closing the reader browser")
            try:
                self._session.close()
            finally:
                self._session = None


class PlaywrightSession:
    """A headless reader browser logged into Teams with a captured chatsvc auth.

    Constructed on the worker thread (Playwright's sync API is thread-bound).
    """

    def __init__(self, interactive: bool = False, headless: bool = True, timeout_ms: int | None = None) -> None:
        from playwright.sync_api import sync_playwright

        from teams_reader.browser import autologin, session

        self._session_mod = session
        self._pw = sync_playwright().start()
        try:
            self.context = session.launch_context(self._pw, headless=headless)
            # A healthy session captures in ~25s; a logged-out one only redirects
            # to sign-in after ~90s, so background opens give up sooner.
            if timeout_ms is None:
                from teams_reader import settings

                timeout_ms = session.open_timeout_ms(interactive, settings.active().load_timeout)
            self.template_url, self._headers = session.capture_request(
                self.context, timeout_ms=timeout_ms,
                autologin=autologin.make_autologin() if interactive else None,
                require_session=not interactive,
                can_prompt=not headless,
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            session.park(self.page)  # API calls below need no running Teams app
        except BaseException:
            context = getattr(self, "context", None)
            if context is not None:
                try:
                    context.close()  # releases the profile lock for the next attempt
                except Exception:  # noqa: BLE001 - still stop Playwright below
                    pass
            self._pw.stop()
            raise

    def fetch(self, url: str) -> object:
        return checked(self._session_mod.fetch_api(self.context, url, self._headers))

    def alive(self) -> bool:
        if self.page.is_closed():
            return False
        self.page.evaluate("1")  # raises once the browser is gone
        return True

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._pw.stop()
