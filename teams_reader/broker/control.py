"""Find and stop a profile's broker, for commands that replace its code or data."""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

_LOCK_NAME = "broker.lock"


def running_pid(data_dir: Path) -> int | None:
    """The pid of the broker serving this profile, or None when none runs."""
    path = data_dir / _LOCK_NAME
    if not path.is_file():
        return None
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raw = os.read(fd, 32).decode(errors="ignore").strip()
            return int(raw) if raw.isdigit() else None
        return None  # nobody holds the lock: the recorded pid is stale
    finally:
        os.close(fd)


def stop(data_dir: Path, timeout: float = 20.0) -> bool:
    """Ask the profile's broker to exit and wait until it released the browser."""
    pid = running_pid(data_dir)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if running_pid(data_dir) is None:
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)  # the OS releases its locks when it dies
    return True


def mcp_server_pids() -> list[int]:
    """Other running ``teams-reader mcp`` servers (each holds the store open)."""
    out = subprocess.run(["pgrep", "-f", "teams-reader mcp"], capture_output=True, text=True).stdout
    return [int(p) for p in out.split() if p.isdigit() and int(p) != os.getpid()]
