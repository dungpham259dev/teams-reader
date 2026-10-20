"""Client side of the broker: what an MCP server uses instead of its own browser."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from teams_reader.browser.backfill import BackfillSummary

# macOS caps a Unix socket path at 104 bytes.
_MAX_SOCKET_PATH = 100
# A little above the broker's own job timeout, so its timeout reply arrives first.
_REPLY_TIMEOUT_S = 330.0
_RECV_CHUNK = 1 << 16
_RESPAWN_EVERY_S = 2.0
_UNREACHABLE = (FileNotFoundError, ConnectionRefusedError, ConnectionResetError)


class BrokerError(RuntimeError):
    def __init__(self, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind


def socket_path(data_dir: Path) -> Path:
    path = data_dir / "broker.sock"
    if len(str(path)) <= _MAX_SOCKET_PATH:
        return path
    # macOS's $TMPDIR is per user (0700), so the fallback is not shared with other users.
    digest = hashlib.sha1(str(data_dir).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"teams-reader-{digest}.sock"


def owned_by_me(path: Path) -> bool:
    try:
        return path.stat().st_uid == os.getuid()
    except FileNotFoundError:
        return False


def spawn_broker(profile_name: str) -> None:
    """Start ``teams-reader broker`` detached, so it outlives this MCP server."""
    subprocess.Popen(
        [sys.executable, "-m", "teams_reader.cli", "--profile", profile_name, "broker"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True,
    )


class BrokerClient:
    def __init__(self, sock_path: Path, spawn: Callable[[], None], connect_timeout: float = 30.0) -> None:
        self._sock_path = sock_path
        self._spawn = spawn
        self._connect_timeout = connect_timeout

    def job(self, name: str, interactive: bool = True, **args: Any) -> Any:
        return self._call({"op": "job", "job": name, "args": args, "interactive": interactive})

    def touch(self, fresh: bool = False) -> None:
        self._call({"op": "touch", "fresh": fresh})

    def status(self) -> dict[str, Any]:
        return self._call({"op": "status"})

    def _call(self, request: dict[str, Any]) -> Any:
        deadline = time.monotonic() + self._connect_timeout
        next_spawn = 0.0
        while True:
            try:
                reply = self._exchange(self._open(), request)
            except _UNREACHABLE:
                reply = None
            if reply is not None and reply.get("type") != "ShuttingDown":
                break
            # No broker, or one on its way out: (re)start one; extras exit on the lock.
            now = time.monotonic()
            if now >= deadline:
                raise BrokerError(
                    f"the teams-reader broker did not start within {self._connect_timeout:.0f}s"
                )
            if now >= next_spawn:
                self._spawn()
                next_spawn = now + _RESPAWN_EVERY_S
            time.sleep(0.1)
        if "error" in reply:
            raise BrokerError(str(reply["error"]), reply.get("type"))
        return reply.get("ok")

    def _open(self) -> socket.socket:
        if self._sock_path.exists() and not owned_by_me(self._sock_path):
            raise BrokerError(f"refusing {self._sock_path}: owned by another user")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self._sock_path))
        except OSError:
            sock.close()
            raise
        return sock

    @staticmethod
    def _exchange(sock: socket.socket, request: dict[str, Any]) -> dict[str, Any]:
        with sock:
            sock.settimeout(_REPLY_TIMEOUT_S)
            sock.sendall(json.dumps(request).encode() + b"\n")
            chunks: list[bytes] = []
            while not chunks or not chunks[-1].endswith(b"\n"):
                chunk = sock.recv(_RECV_CHUNK)
                if not chunk:
                    break
                chunks.append(chunk)
        if not chunks:
            raise ConnectionResetError("broker closed the connection before replying")
        try:
            reply = json.loads(b"".join(chunks))
        except ValueError as exc:
            raise BrokerError(f"broker closed the connection mid-reply: {exc}") from exc
        if not isinstance(reply, dict):
            raise BrokerError("broker sent a malformed reply")
        return reply


class LiveProxy:
    """The slice of ``LiveSync`` the MCP server uses, served by the broker."""

    def __init__(self, client: BrokerClient) -> None:
        self._client = client

    def touch(self, fresh: bool = False) -> None:
        self._client.touch(fresh=fresh)

    def job(self, name: str, **args: Any) -> Any:
        return self._client.job(name, **args)

    def sync(self, **kwargs: Any) -> BackfillSummary:
        return BackfillSummary(**self._client.job("sync", **kwargs))

    def status(self) -> dict[str, Any]:
        return self._client.status()

    def close(self) -> None:
        """Nothing to close: the broker is shared and exits when idle."""
