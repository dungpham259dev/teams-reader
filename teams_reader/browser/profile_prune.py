"""Keep the reader browser's Service Worker CacheStorage from piling up.

Teams web precaches its whole client per origin (teams.microsoft.com and
teams.cloud.microsoft each kept one) and never drops old client versions, so
the profile grew to ~700 MB. Pruning runs before launch, while the profile
lock is held and no browser has the files open.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

CACHE_STORAGE = Path("Default") / "Service Worker" / "CacheStorage"
# One Teams client precache is ~250-380 MB; above this, old versions have piled up.
MAX_KEPT_BYTES = 450 * 1024 * 1024

# Boot starts on teams.microsoft.com but the app runs here, so that origin's precache is the one reused.
APP_ORIGIN = "https://teams.cloud.microsoft/"

_ORIGIN = re.compile(rb"https://[A-Za-z0-9.-]+/")


@dataclass(frozen=True)
class OriginCache:
    path: Path
    origin: str
    size: int
    mtime: float


@dataclass(frozen=True)
class PruneResult:
    removed: tuple[OriginCache, ...]
    kept: OriginCache | None

    @property
    def freed(self) -> int:
        return sum(cache.size for cache in self.removed)


def origin_of(cache_dir: Path) -> str:
    """The origin a CacheStorage folder belongs to, read from its index ("" if unknown)."""
    try:
        match = _ORIGIN.search((cache_dir / "index.txt").read_bytes())
    except OSError:
        return ""
    return match.group().decode() if match else ""


def _scan(cache_dir: Path) -> OriginCache:
    files = [path for path in cache_dir.rglob("*") if path.is_file()]
    stats = [path.stat() for path in files]
    return OriginCache(
        path=cache_dir,
        origin=origin_of(cache_dir),
        size=sum(stat.st_size for stat in stats),
        mtime=max((stat.st_mtime for stat in stats), default=0.0),
    )


def _is_teams(cache: OriginCache) -> bool:
    return cache.origin.startswith("https://teams.")


def plan(caches: list[OriginCache], max_kept: int = MAX_KEPT_BYTES) -> PruneResult:
    """Keep only the Teams app origin's cache, and only while it is not bloated."""
    teams = [cache for cache in caches if _is_teams(cache)]
    preferred = max(teams, key=lambda cache: (cache.origin == APP_ORIGIN, cache.mtime), default=None)
    kept = preferred if preferred is not None and preferred.size <= max_kept else None
    removed = tuple(cache for cache in caches if cache is not kept)
    return PruneResult(removed=removed, kept=kept)


def prune(browser_dir: Path, max_kept: int = MAX_KEPT_BYTES) -> PruneResult:
    root = browser_dir / CACHE_STORAGE
    if not root.is_dir():
        return PruneResult(removed=(), kept=None)
    caches = [_scan(path) for path in sorted(root.iterdir()) if path.is_dir()]
    result = plan(caches, max_kept)
    for cache in result.removed:
        shutil.rmtree(cache.path, ignore_errors=True)
    return result
