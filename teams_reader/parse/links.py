"""Pure extraction of web links from a message's HTML content.

Teams shows long links truncated with an ellipsis, so the anchor's ``href`` is
the source of truth; bare URLs in the text are picked up as well. Teams image
objects are excluded (they are handled as images).
"""
from __future__ import annotations

import html
import re

_HREF_RE = re.compile(r"""<a\b[^>]*\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BARE_RE = re.compile(r"https?://[^\s<>\"']+")
_TRAILING = ".,;:!?)]}"


def _clean(url: str) -> str | None:
    url = html.unescape(url).strip().rstrip(_TRAILING)
    if not url.lower().startswith(("http://", "https://")) or url.endswith("…"):
        return None
    if "/v1/objects/" in url:
        return None
    return url


def extract_link_urls(content: str) -> list[str]:
    if not content:
        return []
    found = [m.group(1) for m in _HREF_RE.finditer(content)]
    text = _TAG_RE.sub(" ", _ANCHOR_RE.sub(" ", content))
    found += [m.group(0) for m in _BARE_RE.finditer(html.unescape(text))]
    out: dict[str, None] = {}
    for raw in found:
        url = _clean(raw)
        if url:
            out.setdefault(url, None)
    return list(out)
