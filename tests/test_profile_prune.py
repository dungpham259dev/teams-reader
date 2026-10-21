import os
from pathlib import Path

import pytest

from teams_reader.browser import profile_prune
from teams_reader.browser.profile_prune import OriginCache

MB = 1024 * 1024


def _origin_dir(root: Path, name: str, origin: str, size: int, mtime: float) -> Path:
    path = root / profile_prune.CACHE_STORAGE / name
    (path / "cache-uuid").mkdir(parents=True)
    (path / "index.txt").write_bytes(b"\x08\x01\x12\x1c" + origin.encode() + b"\x1a\x1c" + origin.encode() + b" \x01(\x01")
    blob = path / "cache-uuid" / "entry_0"
    blob.write_bytes(b"x" * size)
    for file in (blob, path / "index.txt"):
        os.utime(file, (mtime, mtime))
    return path


def _cache(origin: str, size: int, mtime: float) -> OriginCache:
    return OriginCache(path=Path(origin), origin=origin, size=size, mtime=mtime)


@pytest.mark.unit
def test_origin_is_read_from_the_cache_index(tmp_path):
    path = _origin_dir(tmp_path, "abc", "https://teams.cloud.microsoft/", 10, 1.0)
    assert profile_prune.origin_of(path) == "https://teams.cloud.microsoft/"
    assert profile_prune.origin_of(tmp_path) == ""


@pytest.mark.unit
def test_plan_keeps_only_the_newest_teams_origin():
    old = _cache("https://teams.microsoft.com/", 380 * MB, 100.0)
    new = _cache("https://teams.cloud.microsoft/", 260 * MB, 200.0)
    other = _cache("https://contoso-my.sharepoint.com/", 50 * MB, 300.0)
    result = profile_prune.plan([old, new, other])
    assert result.kept is new
    assert set(result.removed) == {old, other}
    assert result.freed == 430 * MB


@pytest.mark.unit
def test_plan_prefers_the_app_origin_over_a_newer_boot_origin():
    app = _cache("https://teams.cloud.microsoft/", 260 * MB, 100.0)
    boot = _cache("https://teams.microsoft.com/", 66 * MB, 200.0)
    assert profile_prune.plan([app, boot]).kept is app


@pytest.mark.unit
def test_plan_falls_back_to_the_newest_teams_origin():
    old = _cache("https://teams.microsoft.com/", 10 * MB, 100.0)
    new = _cache("https://teams.live.com/", 10 * MB, 200.0)
    assert profile_prune.plan([old, new]).kept is new


@pytest.mark.unit
def test_plan_drops_a_bloated_teams_cache_too():
    bloated = _cache("https://teams.cloud.microsoft/", 600 * MB, 200.0)
    result = profile_prune.plan([bloated], max_kept=450 * MB)
    assert result.kept is None
    assert result.removed == (bloated,)


@pytest.mark.unit
def test_plan_with_nothing_to_prune():
    only = _cache("https://teams.cloud.microsoft/", 10 * MB, 1.0)
    assert profile_prune.plan([only]) == profile_prune.PruneResult(removed=(), kept=only)
    assert profile_prune.plan([]) == profile_prune.PruneResult(removed=(), kept=None)


@pytest.mark.unit
def test_prune_deletes_stale_origin_folders_on_disk(tmp_path):
    stale = _origin_dir(tmp_path, "aaa", "https://teams.microsoft.com/", 300, 200.0)
    current = _origin_dir(tmp_path, "bbb", "https://teams.cloud.microsoft/", 200, 100.0)
    result = profile_prune.prune(tmp_path, max_kept=1000)
    assert not stale.exists()
    assert current.exists()
    assert result.kept.path == current
    assert result.freed > 300


@pytest.mark.unit
def test_prune_without_a_cache_folder_is_a_no_op(tmp_path):
    assert profile_prune.prune(tmp_path) == profile_prune.PruneResult(removed=(), kept=None)
