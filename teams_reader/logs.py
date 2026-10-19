"""Activity log per profile: what the reader did and how long it took.

Records tool calls, cache reads, the warm browser, background sync and sign-in
steps. It holds counts, timings, states and the arguments Claude passed;
never message text, tokens or passwords. Follow it with ``teams-reader logs -f``.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from teams_reader import settings

LOG_FILE = "teams-reader.log"
_MAX_BYTES = 1_000_000
_BACKUPS = 3
_ARG_CHARS = 60
_FORMAT = "%(asctime)s %(levelname)-7s %(name)-10s %(message)s"
_ROOT = "teams_reader"


def log_path(profile: settings.Profile | None = None) -> Path:
    return (profile or settings.active()).data_dir / "logs" / LOG_FILE


def get(name: str) -> logging.Logger:
    """A logger shown as ``name`` in the log (e.g. ``mcp``, ``sync``)."""
    return logging.getLogger(f"{_ROOT}.{name}")


class _ShortName(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.name = record.name.removeprefix(f"{_ROOT}.")
        return super().format(record)


def setup(profile: settings.Profile | None = None, level: str | None = None) -> Path:
    """Send this process's log to the profile's rotating log file."""
    path = log_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    reset()
    handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    handler.setFormatter(_ShortName(_FORMAT, datefmt="%H:%M:%S"))
    root = logging.getLogger(_ROOT)
    root.addHandler(handler)
    root.setLevel((level or os.environ.get("TEAMS_READER_LOG_LEVEL") or "INFO").upper())
    root.propagate = False
    return path


def reset() -> None:
    root = logging.getLogger(_ROOT)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def short_args(args: tuple, kwargs: dict[str, Any]) -> str:
    def show(value: Any) -> str:
        text = repr(value)
        return text if len(text) <= _ARG_CHARS else text[: _ARG_CHARS - 1] + "…"

    parts = [show(a) for a in args]
    parts += [f"{k}={show(v)}" for k, v in kwargs.items() if v is not None]
    return ", ".join(parts)


def summarize(result: Any) -> str:
    """The size of a tool result, never its content."""
    if isinstance(result, list):
        return f"{len(result)} items"
    if isinstance(result, dict):
        if result.get("error"):
            return f"error: {str(result['error'])[:120]}"
        sizes = [f"{k}={len(v)}" for k, v in result.items() if isinstance(v, (list, dict))]
        return ", ".join(sizes) or f"{len(result)} fields"
    return type(result).__name__
