"""The broker process: one warm browser behind a Unix socket.

Requests and replies are one JSON object per line. A second broker for the same
profile exits at once (``broker.lock``), and an idle broker exits on its own,
closing the browser; the next client request starts a new one.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import socketserver
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Callable

from teams_reader import logs
from teams_reader.broker import jobs as jobs_mod

DEFAULT_IDLE_AFTER = 1800.0
# A job queued behind a Teams sign-in waits; this bounds how long a tool call hangs.
JOB_TIMEOUT_S = 300.0
# Covers a cold start (browser ~20-30s) plus one sync; past it the answer comes from the store.
FRESH_TIMEOUT_S = 45.0
_MAX_REQUEST_BYTES = 1 << 20
_READ_TIMEOUT_S = 10.0
_log = logs.get("broker")


class Broker:
    def __init__(self, live: Any, idle_after: float = DEFAULT_IDLE_AFTER,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time) -> None:
        self._live = live
        self._wall = wall
        self._fresh_lock = threading.Lock()
        self._fresh_started: float | None = None
        self._idle_after = idle_after
        self._clock = clock
        self._last_request = clock()
        self._in_flight = 0
        self._closing = False
        self._state = threading.Lock()

    def idle(self) -> bool:
        with self._state:
            return self._idle_locked()

    def _idle_locked(self) -> bool:
        return self._in_flight == 0 and self._clock() - self._last_request >= self._idle_after

    def begin_shutdown_if_idle(self) -> bool:
        """Atomically stop taking work when idle; True when the broker should exit."""
        with self._state:
            if self._closing or self._idle_locked():
                self._closing = True
        return self._closing

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._state:
            if self._closing:
                # Nothing ran, so the client may safely retry on the next broker.
                return {"error": "the broker is shutting down", "type": "ShuttingDown"}
            self._in_flight += 1
            self._last_request = self._clock()
        try:
            return self._dispatch(request)
        finally:
            with self._state:
                self._in_flight -= 1
                self._last_request = self._clock()

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        try:
            if op == "status":
                return {"ok": {**self._live.status(), "pid": os.getpid()}}
            if op == "touch":
                self._live.touch()
                if request.get("fresh"):
                    self._live.recover_login()
                return {"ok": True}
            if op == "job":
                return self._job(request)
            if op == "fresh":
                return self._fresh(float(request.get("since") or 0.0))
        except FutureTimeout:
            return {"error": f"the browser job did not finish within {JOB_TIMEOUT_S:.0f}s "
                             "(a Teams sign-in may be in progress)", "type": "TimeoutError"}
        except Exception as exc:  # noqa: BLE001 - delivered to the client
            return {"error": str(exc), "type": type(exc).__name__}
        return {"error": f"unknown op: {op}"}

    def _fresh(self, since: float) -> dict[str, Any]:
        """Sync before a question is answered; questions asked while one sync
        runs share the next one instead of queueing a sync each."""
        self._live.touch()
        self._live.recover_login()
        with self._fresh_lock:
            if self._fresh_started is not None and self._fresh_started >= since:
                return {"ok": {"synced": False}}
            started = self._wall()
            self._fresh_started = started
            summary = self._live.run(lambda session, conn: jobs_mod.fresh_sync(session, conn, started),
                                     timeout=FRESH_TIMEOUT_S, interactive=False)
        return {"ok": {"synced": True, **summary}}

    def _job(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("job"))
        job = jobs_mod.JOBS.get(name)
        if job is None:
            return {"error": f"unknown job: {name}"}
        args = request.get("args") or {}
        self._live.touch()
        result = self._live.run(lambda session, conn: job(session, conn, args),
                                timeout=JOB_TIMEOUT_S, interactive=bool(request.get("interactive", True)))
        return {"ok": result}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(_READ_TIMEOUT_S)  # a silent client must not pin a thread
        try:
            line = self.rfile.readline(_MAX_REQUEST_BYTES)
        except OSError:
            return
        self.request.settimeout(None)
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except ValueError as exc:
            reply: dict[str, Any] = {"error": f"bad request: {exc}"}
        else:
            reply = self.server.broker.handle(request)  # type: ignore[attr-defined]
        self.wfile.write(json.dumps(reply, default=str).encode() + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def listen(sock_path: Path, broker: Broker) -> _Server:
    if sock_path.exists():
        sock_path.unlink()  # left by a broker that died; the lock proves none is running
    previous = os.umask(0o177)  # private from the first instant, not after a chmod
    try:
        server = _Server(str(sock_path), _Handler)
    finally:
        os.umask(previous)
    server.broker = broker  # type: ignore[attr-defined]
    return server


def _single_instance(data_dir: Path) -> int | None:
    """An flock fd, or None when another broker already serves this profile."""
    fd = os.open(data_dir / "broker.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def serve(data_dir: Path, sock_path: Path, db_file: Path, idle_after: float = DEFAULT_IDLE_AFTER) -> int:
    from teams_reader.mcp import livesync

    data_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = _single_instance(data_dir)
    if lock_fd is None:
        _log.info("another broker already serves this profile; exiting")
        return 0
    live = livesync.LiveSync(db_file)
    broker = Broker(live, idle_after=idle_after)
    server = listen(sock_path, broker)
    _log.info("broker %d listening on %s", os.getpid(), sock_path)

    def stop_when_idle() -> None:
        while not broker.begin_shutdown_if_idle():
            time.sleep(min(30.0, idle_after))
        _log.info("broker idle for %.0fs; closing the browser and exiting", idle_after)
        server.shutdown()

    threading.Thread(target=stop_when_idle, name="broker-idle", daemon=True).start()

    def on_term(_signum: int, _frame: Any) -> None:
        # shutdown() waits for serve_forever, which runs on this thread: call it from another.
        _log.info("broker asked to stop; closing the browser")
        threading.Thread(target=server.shutdown, name="broker-stop", daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        live.close()
        # Unlinked last: a client finding no socket starts a broker, which then gets the lock.
        sock_path.unlink(missing_ok=True)
        os.close(lock_fd)
    return 0
