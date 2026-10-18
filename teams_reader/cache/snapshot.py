"""Copy a live LevelDB directory to a readable snapshot.

Teams holds a write lock on its LevelDB while running, so we copy the directory
and remove the ``LOCK`` file from the copy; the original is never touched.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def snapshot_leveldb(source: Path, dest: Path) -> Path:
    """Copy ``source`` leveldb dir to ``dest`` and drop the copy's LOCK file."""
    if not source.is_dir():
        raise FileNotFoundError(f"LevelDB directory not found: {source}")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name == "LOCK":
            continue
        if item.is_file():
            shutil.copy2(item, dest / item.name)
    return dest
