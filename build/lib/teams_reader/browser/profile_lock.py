"""One process per browser profile.

Two Chromium processes on the same persistent profile hang instead of failing
(headless shell takes no SingletonLock), e.g. a CLI run while the MCP server's
warm browser is up. An flock on ``browser.lock`` makes the second one fail fast;
the OS releases it when the holder exits, so a crash never leaves it stuck.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path

_LOCK_NAME = "browser.lock"


class ProfileInUse(RuntimeError):
    pass


class ProfileLock:
    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


def is_held(directory: Path) -> bool:
    """Whether some process (this one included) holds the profile's browser."""
    path = directory / _LOCK_NAME
    if not path.is_file():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # closing drops a lock we just took
    return False


def acquire(directory: Path, profile_name: str) -> ProfileLock:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _LOCK_NAME
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        owner = os.read(fd, 32).decode(errors="ignore").strip() or "unknown"
        os.close(fd)
        raise ProfileInUse(
            f"browser profile {profile_name!r} is in use by another teams-reader process "
            f"(pid {owner}), e.g. the MCP server's warm browser; stop it or use another profile"
        ) from None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return ProfileLock(fd)
