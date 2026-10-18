"""Spike: can we fetch a Teams image's bytes via the logged-in web session?

Images live on the AMS service (``*.asm.skype.com/v1/objects/<id>/views/imgo``),
which may want a different auth header than chatsvc. This logs in (reusing the
saved profile), scans chats for one image URL, then tries to fetch its bytes two
ways — the captured chatsvc headers, and an AMS-style ``Authorization:
skype_token <token>`` — and reports which (if any) returns image bytes.

Run it yourself (authenticated fetch is blocked for the agent):
    cd ~/Code/teams-reader && venv/bin/python tools/try_image.py
"""
from __future__ import annotations

import base64
import re
import sys

from playwright.sync_api import sync_playwright

from teams_reader import config
from teams_reader.browser import chatsvc, session
from teams_reader.store import db as db_mod

_IMGO_RE = re.compile(r"https://[^\"'<>\\ ]+/v1/objects/[^\"'<>\\ ]+/views/imgo")
_OBJ_RE = re.compile(r"https://[^\"'<>\\ ]+/v1/objects/[^\"'<>\\ ]+")
_TOKEN_RE = re.compile(r"skypetoken=([^;,\s\"]+)")



def _find_image_url(page, template, headers, chats) -> str | None:
    for row in chats:
        chat_id = row["chat_id"]
        if not chat_id:
            continue
        payload = session.fetch_json(page, chatsvc.with_conversation(template, chat_id), headers)
        if not isinstance(payload, dict):
            continue
        blob = str(payload)
        match = _IMGO_RE.search(blob) or _OBJ_RE.search(blob)
        if match:
            print(f"  found image in chat: {row['display_name'] or chat_id}")
            return match.group(0)
    return None


def _try_fetch(context, url, headers, label) -> bool:
    # context.request runs at the browser layer (no CORS), unlike page fetch().
    print(f"\n[{label}] fetching {url[:90]}...")
    try:
        resp = context.request.get(url, headers=headers, timeout=60_000)
    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR: {exc}")
        return False
    ct = resp.headers.get("content-type")
    body = resp.body()
    print(f"  status={resp.status} content-type={ct} size={len(body)}")
    if resp.status != 200 or not body:
        return False
    out = f"/tmp/teams_test_image_{label}"
    ext = ".jpg" if "jpeg" in (ct or "") else ".png" if "png" in (ct or "") else ".bin"
    with open(out + ext, "wb") as handle:
        handle.write(body)
    print(f"  SAVED {out + ext} ({len(body)} bytes, head={body[:4].hex()})")
    return True


def main() -> int:
    with sync_playwright() as pw:
        # Headful + keep the window focused: Teams web is unreliable headless.
        context = session.launch_context(pw, headless=False)
        try:
            template, headers = session.capture_request(context)
            page = context.pages[0] if context.pages else context.new_page()
            session.settle(page)

            conn = db_mod.connect(config.db_path())
            img_url = _find_image_url(page, template, headers, db_mod.list_chats(conn))
            if not img_url:
                print("no image URL found in any chat's first page")
                return 2
            if "/views/" not in img_url:
                img_url = img_url.rstrip("/") + "/views/imgo"

            token_match = _TOKEN_RE.search(str(headers))
            token = token_match.group(1) if token_match else None
            print(f"skypetoken found: {bool(token)}")

            attempts = []
            if token:
                attempts.append(("ams-skype_token", {"Authorization": f"skype_token {token}"}))
                attempts.append(("ams-bearer", {"Authorization": f"Bearer {token}"}))
            attempts.append(("chatsvc-headers", headers))

            ok = False
            for label, hdrs in attempts:
                if _try_fetch(context, img_url, hdrs, label):
                    ok = True
                    break

            print("\nRESULT:", "image bytes fetched OK" if ok else "could not fetch image bytes")
            return 0 if ok else 3
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
