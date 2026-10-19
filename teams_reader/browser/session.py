"""Playwright session for the Teams web client, optionally through a proxy (e.g. a VPN).

Everything here needs a real browser and a logged-in user, so it is driven
interactively (``teams-reader login`` / ``backfill``), not unit-tested. The VPN
is a local SOCKS5 proxy; Chromium reaches Teams and chatsvc through it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from teams_reader import settings
from teams_reader.browser import chatsvc

TEAMS_WEB_URL = "https://teams.microsoft.com/v2/"

# Headers the browser sets itself; replaying them breaks the fetch.
_DROP_HEADERS = {"host", "content-length", "connection", "accept-encoding"}

_FETCH_JS = """
async ({url, headers}) => {
    try {
        const r = await fetch(url, {headers, credentials: 'include'});
        if (!r.ok) return {error: r.status};
        return await r.json();
    } catch (e) {
        return {error: String(e)};
    }
}
"""


# The reader must never act as the user: no read receipts ("Seen"), no presence
# or activity reports. The Teams web app tries these on its own once it opens a
# chat, so they are refused at the network layer for every page we drive.
_BLOCKED_WRITES = re.compile(
    r"consumptionhorizon|reportmyactivity|presenceDocs|forceavailability|setpresence|/endpoints/[^/]+/active",
    re.IGNORECASE,
)


def blocks_write(method: str, url: str) -> bool:
    return method.upper() != "GET" and bool(_BLOCKED_WRITES.search(url))


def _guard_writes(context: Any) -> None:
    def handle(route: Any) -> None:
        if blocks_write(route.request.method, route.request.url):
            route.abort()
        else:
            route.continue_()

    context.route(_BLOCKED_WRITES, handle)


def fetch_api(context: Any, url: str, headers: dict[str, str], timeout_ms: int = 60_000) -> object:
    """GET a chatsvc URL outside any page (the Teams app need not be running)."""
    try:
        response = context.request.get(url, headers=headers, timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - same error shape as the in-page fetch
        return {"error": str(exc)}
    if not response.ok:
        return {"error": response.status}
    return response.json()


def park(page: Any) -> None:
    """Unload the Teams app once auth is captured, so no chat stays open."""
    try:
        page.goto("about:blank")
    except Exception:  # noqa: BLE001 - parking is best-effort; writes stay blocked anyway
        pass


def sign_in_blocker(require_session: bool, autologin: Any, can_prompt: bool) -> str | None:
    """Why a sign-in page cannot be completed here (None = it can be)."""
    if require_session:
        return "Teams session expired; call sync_history or run `teams-reader login`"
    if autologin is None and not can_prompt:
        return (
            "Teams web sign-in is needed and no credentials are configured; "
            "run `teams-reader login` to sign in once in a visible window"
        )
    return None


def proxy_server() -> str | None:
    """The active profile's proxy (e.g. a VPN's SOCKS5), or None for direct."""
    return settings.active().proxy


def launch_context(playwright: Any, headless: bool = False) -> Any:
    """A persistent Chromium context so the Teams login survives between runs."""
    from teams_reader.browser import profile_lock

    profile = settings.active()
    lock = profile_lock.acquire(profile.data_dir, profile.name)  # fail fast, don't hang
    options: dict[str, Any] = {"headless": headless, "viewport": {"width": 1280, "height": 900}}
    if proxy_server():
        options["proxy"] = {"server": proxy_server()}
    try:
        profile.browser_dir.mkdir(parents=True, exist_ok=True)
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile.browser_dir), **options
        )
    except BaseException:
        lock.release()
        raise
    context.on("close", lambda _ctx: lock.release())
    _guard_writes(context)
    return context


def _page_for(context: Any) -> Any:
    return context.pages[0] if context.pages else context.new_page()


def _clean_headers(raw: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in raw.items()
        if not key.startswith(":") and key.lower() not in _DROP_HEADERS
    }


# Let the SPA boot fully (it fires chatsvc within ~30s on a healthy session);
# only reload as a last resort, well after that, if nothing has fired.
_RELOAD_EVERY_MS = 100_000


def _open(page: Any) -> None:
    """Start loading Teams without blocking on full load (it redirects a lot)."""
    try:
        page.goto(TEAMS_WEB_URL, wait_until="commit", timeout=60_000)
    except Exception:  # noqa: BLE001 - a slow/aborted navigation must not stop capture
        pass


# The conversations-list template works (pageSize=200), so don't wait for /messages.
_PREFER_MESSAGES_MS = 0


def capture_request(
    context: Any,
    timeout_ms: int = 180_000,
    autologin: Any = None,
    require_session: bool = False,
    can_prompt: bool = True,
) -> tuple[str, dict[str, str]]:
    """Navigate to Teams and capture a chatsvc request's URL template + headers.

    Prefers a real ``/messages`` call (exact template), but falls back to the
    conversations-list call that always fires on boot, constructing a messages
    template from its host. The headers (session auth token) are replayed later
    by :func:`fetch_api`. On timeout it saves a screenshot for diagnosis.

    On a sign-in page: ``autologin`` (an :class:`AutoLogin`) fills it in step by
    step; with ``require_session`` (background work) it fails fast instead, so
    nothing unattended ever triggers an MFA prompt.
    """
    import time

    from teams_reader.browser import autologin as autologin_mod
    from teams_reader.browser.live import LoginRequired

    captured: dict[str, Any] = {}

    def on_request(req: Any) -> None:
        url = req.url
        if chatsvc.is_messages_url(url):
            captured.setdefault("messages", req)
        elif chatsvc.is_conversations_url(url):
            captured.setdefault("conversations", req)

    failed_navigation: dict[str, str] = {}

    def on_request_failed(req: Any) -> None:
        if req.is_navigation_request():
            failed_navigation["url"], failed_navigation["error"] = req.url, str(req.failure)

    context.on("request", on_request)
    context.on("requestfailed", on_request_failed)
    page = _page_for(context)
    start = time.monotonic()
    last_reload_ms = 0.0
    conv_seen_ms: float | None = None
    signing_in = False
    _open(page)
    try:
        while "messages" not in captured:
            elapsed_ms = (time.monotonic() - start) * 1000
            if "conversations" in captured:
                if conv_seen_ms is None:
                    conv_seen_ms = elapsed_ms
                if elapsed_ms - conv_seen_ms >= _PREFER_MESSAGES_MS:
                    break  # use the conversations-list fallback
            if elapsed_ms >= timeout_ms:
                if "conversations" in captured:
                    break
                _screenshot(page)
                where = ""
                try:
                    where = f" (page at: {page.url})"
                except Exception:  # noqa: BLE001
                    pass
                raise TimeoutError(
                    "No chatsvc request seen; saved a screenshot to "
                    f"{_fail_screenshot()}. Log in in the window, or re-run "
                    f"`teams-reader login`.{where}"
                )
            if page.url.startswith("chrome-error://") and failed_navigation:
                raise autologin_mod.LoginFailed(
                    autologin_mod.navigation_failure_message(failed_navigation["url"], failed_navigation["error"])
                )
            if autologin_mod.on_login_page(page.url):
                blocker = sign_in_blocker(require_session, autologin, can_prompt)
                if blocker:
                    raise LoginRequired(blocker)  # a hidden page nobody can sign in to
                if not signing_in:
                    signing_in = True
                    start = time.monotonic()  # fresh budget for sign-in (incl. MFA)
                if autologin is not None:
                    autologin.step(page, autologin_mod.visible_keys(page), autologin_mod.read_text(page))
                page.wait_for_timeout(1_000)
                continue  # never reload mid sign-in
            if signing_in:
                # Signed in: give the app (a cold first boot over a VPN) a fresh budget.
                signing_in = False
                start = time.monotonic()
                last_reload_ms = 0.0
                continue
            if elapsed_ms - last_reload_ms >= _RELOAD_EVERY_MS:
                last_reload_ms = elapsed_ms
                _open(page)
            page.wait_for_timeout(1_000)
    finally:
        context.remove_listener("request", on_request)
        context.remove_listener("requestfailed", on_request_failed)

    if "messages" in captured:
        request = captured["messages"]
        return request.url, _clean_headers(request.all_headers())
    request = captured["conversations"]
    return chatsvc.messages_template(request.url), _clean_headers(request.all_headers())


def _fail_screenshot() -> Path:
    return settings.active().data_dir / "capture_fail.png"


def _screenshot(page: Any) -> None:
    try:
        settings.active().data_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(_fail_screenshot()))
    except Exception:  # noqa: BLE001 - diagnosis only
        pass


def settle(page: Any, timeout_ms: int = 15_000) -> None:
    """Wait for the SPA's boot navigations to finish before evaluating in it."""
    for state in ("load", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - networkidle may never fire on a live SPA
            pass


def fetch_json(page: Any, url: str, headers: dict[str, str], retries: int = 3) -> object:
    """Fetch a chatsvc URL from inside the page, replaying the captured headers.

    Retries when a page navigation destroys the JS context mid-fetch (common
    while the Teams web app is still booting).
    """
    from playwright.sync_api import Error as PlaywrightError

    for attempt in range(retries + 1):
        try:
            return page.evaluate(_FETCH_JS, {"url": url, "headers": headers})
        except PlaywrightError as exc:
            if "context was destroyed" in str(exc).lower() and attempt < retries:
                page.wait_for_timeout(2_000)
                continue
            raise


def login(timeout_ms: int = 300_000) -> bool:
    """Open Teams web (headful) so the user logs in once; persist the profile."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = launch_context(pw, headless=False)
        try:
            from teams_reader.browser.autologin import make_autologin

            url, _ = capture_request(context, timeout_ms=timeout_ms, autologin=make_autologin())
            return chatsvc.is_messages_url(url)
        finally:
            context.close()
