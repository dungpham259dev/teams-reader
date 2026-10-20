"""Teams image handling.

Messages reference images by URL on the AMS service
(``https://<host>/v1/objects/<id>/views/imgo``); the bytes are fetched through
the logged-in browser context (cookie auth, no CORS — unlike a page ``fetch``).

``extract_image_urls`` is pure/tested. ``download_images`` drives the browser
and is run interactively (same session as the backfill).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from teams_reader import settings
from teams_reader.parse.images import extract_image_urls  # re-exported

__all__ = ["extract_image_urls", "local_path_for", "download_images"]

def local_path_for(url: str) -> Path:
    """A stable local file path for an image URL (named by its hash)."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    return settings.active().images_dir / f"{digest}.jpg"


def download_with(
    conn: sqlite3.Connection,
    context: object,
    *,
    chat: str | None = None,
    msg_ids: list[str] | None = None,
    limit: int = 50,
    headers: dict[str, str] | None = None,
) -> dict[str, int]:
    """Fetch pending image bytes through an open, logged-in browser context."""
    from teams_reader.store import db as db_mod

    targets = db_mod.pending_images(conn, chat_filter=chat, msg_ids=msg_ids, limit=limit)
    settings.active().images_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for msg_id, url in targets:
        dest = local_path_for(url)
        try:
            resp = context.request.get(url, timeout=60_000)
            # Some objects (e.g. from group-chat calls) want the session's auth header, not just cookies.
            if resp.status in (401, 403) and headers:
                resp = context.request.get(url, timeout=60_000, headers=headers)
        except Exception:  # noqa: BLE001 - one bad image must not abort the batch
            continue
        if resp.status in (401, 403):
            from teams_reader.browser.live import AuthExpired

            raise AuthExpired(f"image fetch returned {resp.status}")
        if resp.status != 200:
            continue
        dest.write_bytes(resp.body())
        db_mod.set_image_path(conn, msg_id, url, str(dest))
        downloaded += 1
    return {"requested": len(targets), "downloaded": downloaded}


def download_images(
    conn: sqlite3.Connection,
    *,
    chat: str | None = None,
    msg_ids: list[str] | None = None,
    headless: bool = True,
    limit: int = 50,
) -> dict[str, int]:
    """One-shot download: launch the browser, warm its cookies, :func:`download_with`.

    Needs a prior ``teams-reader login``. Imports Playwright lazily.
    """
    from playwright.sync_api import sync_playwright

    from teams_reader.browser import session
    from teams_reader.store import db as db_mod

    if not db_mod.pending_images(conn, chat_filter=chat, msg_ids=msg_ids, limit=limit):
        return {"requested": 0, "downloaded": 0}
    with sync_playwright() as pw:
        context = session.launch_context(pw, headless=headless)
        try:
            from teams_reader.browser.autologin import make_autologin

            # logged in + warm cookies
            session.capture_request(context, autologin=make_autologin(), can_prompt=not headless)
            return download_with(conn, context, chat=chat, msg_ids=msg_ids, limit=limit)
        finally:
            context.close()
