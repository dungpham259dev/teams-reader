"""Pure extraction of image references from a message's HTML content.

Teams embeds images as AMS object URLs
(``https://<host>/v1/objects/<id>/views/img{o,t,...}``). We normalise every
reference to the full-size ``imgo`` view and de-duplicate.
"""
from __future__ import annotations

import re

_OBJECT_RE = re.compile(r"https://[^\"'\s<>]+?/v1/objects/[^\"'\s<>/]+")


def extract_image_urls(content: str) -> list[str]:
    if not content:
        return []
    out: dict[str, None] = {}
    for match in _OBJECT_RE.finditer(content):
        out.setdefault(match.group(0) + "/views/imgo", None)
    return list(out)
