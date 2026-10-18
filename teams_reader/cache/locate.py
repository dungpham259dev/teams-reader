"""Locate the Teams IndexedDB LevelDB directories on disk."""
from __future__ import annotations

from pathlib import Path

from teams_reader import config

_LEVELDB_SUFFIX = ".indexeddb.leveldb"


def find_leveldbs(base: Path | None = None) -> list[Path]:
    """All ``*.indexeddb.leveldb`` dirs under the Teams EBWebView tree."""
    root = base if base is not None else config.TEAMS_EBWEBVIEW
    if not root.exists():
        return []
    return sorted(p for p in root.rglob(f"*{_LEVELDB_SUFFIX}") if p.is_dir())


def work_leveldb(base: Path | None = None) -> Path | None:
    """The work/school (teams.microsoft.com) message database, if present."""
    for db in find_leveldbs(base):
        if config.PROFILE_WORK in db.parts and "teams.microsoft.com" in db.name:
            return db
    return None


def for_cache(cache: str, base: Path | None = None) -> Path | None:
    """The LevelDB a profile's ``cache`` setting points at (None when disabled)."""
    if cache == "work":
        return work_leveldb(base)
    if cache == "personal":
        return personal_leveldb(base)
    if cache == "none":
        return None
    path = Path(cache)
    return path if path.is_dir() else None


def active_leveldb() -> Path | None:
    from teams_reader import settings

    return for_cache(settings.active().cache)


def personal_leveldb(base: Path | None = None) -> Path | None:
    """The consumer (teams.live.com) message database, if present."""
    for db in find_leveldbs(base):
        if config.PROFILE_PERSONAL in db.parts and "teams.live.com" in db.name:
            return db
    return None
