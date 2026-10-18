"""Decode a LevelDB snapshot into JSONL using dfindexeddb.

The command builder is kept pure and the executor is injectable so the pipeline
can be tested without invoking the real binary.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

Runner = Callable[[list[str]], str]


def _default_bin() -> str:
    """Prefer the dfindexeddb installed alongside this interpreter (venv), then
    an explicit override, then PATH — so the installed CLI works without the
    venv's bin dir being on PATH."""
    override = os.environ.get("TEAMS_READER_DFINDEXEDDB")
    if override:
        return override
    sibling = Path(sys.executable).with_name("dfindexeddb")
    return str(sibling) if sibling.exists() else "dfindexeddb"


DEFAULT_BIN = _default_bin()


def build_command(snapshot_dir: Path, fmt: str = "chromium", binary: str = DEFAULT_BIN) -> list[str]:
    return [binary, "db", "-s", str(snapshot_dir), "-f", fmt, "-o", "jsonl"]


def _subprocess_runner(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def dump_to_jsonl(
    snapshot_dir: Path,
    out_path: Path,
    fmt: str = "chromium",
    binary: str = DEFAULT_BIN,
    runner: Runner = _subprocess_runner,
) -> Path:
    """Run dfindexeddb over ``snapshot_dir`` and write JSONL to ``out_path``.

    Reads the compacted ``.ldb`` data. The write-ahead ``.log`` is intentionally
    not parsed: it only holds per-chat "last message" summaries as raw blobs, not
    full message records, so it is not worth the fragile blob-mining. Freshest
    messages arrive once Teams compacts, or via the browser sync path.
    """
    stdout = runner(build_command(snapshot_dir, fmt=fmt, binary=binary))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(stdout, encoding="utf-8")
    return out_path
