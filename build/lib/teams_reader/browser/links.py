"""Open links shared in chats through the logged-in reader browser (GET only).

The page is navigated and its readable text extracted; nothing is clicked or
submitted. ``open_link`` shows the same persistent profile so the user can log
in to a site (Confluence, GitLab) once.
"""
from __future__ import annotations

from typing import Any

_EXTRACT_JS = """() => {
  const pick = document.querySelector('main, article, [role=main], #main-content, #content');
  const node = pick && pick.innerText.trim().length > 200 ? pick : document.body;
  return node ? node.innerText : '';
}"""
_SETTLE_MS = 10_000


def fetch_page_with(context: Any, url: str, timeout_ms: int = 45_000) -> dict[str, Any]:
    """Load ``url`` in a fresh tab of an open context; return URL, title, text."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
        except Exception:  # noqa: BLE001 - SPAs may never go idle
            pass
        return {"final_url": page.url, "title": page.title(), "text": page.evaluate(_EXTRACT_JS)}
    finally:
        page.close()


def fetch_page(url: str, headless: bool = True, timeout_ms: int = 45_000) -> dict[str, Any]:
    """One-shot: launch the reader browser and :func:`fetch_page_with`."""
    from playwright.sync_api import sync_playwright

    from teams_reader.browser import session

    with sync_playwright() as pw:
        context = session.launch_context(pw, headless=headless)
        try:
            return fetch_page_with(context, url, timeout_ms=timeout_ms)
        finally:
            context.close()


def open_link(url: str) -> None:
    """Show ``url`` in the reader's browser until the window is closed."""
    from playwright.sync_api import sync_playwright

    from teams_reader.browser import session

    with sync_playwright() as pw:
        context = session.launch_context(pw, headless=False)
        try:
            page = context.new_page()
            page.goto(url, wait_until="commit", timeout=60_000)
            page.wait_for_event("close", timeout=0)
        finally:
            context.close()
