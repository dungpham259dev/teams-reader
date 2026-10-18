import pytest

from teams_reader.cache import dump as dump_mod
from teams_reader.cache import locate as locate_mod
from teams_reader.cache.snapshot import snapshot_leveldb


def _make_leveldb(root, profile, host, files):
    db = root / profile / "IndexedDB" / f"https_{host}_0.indexeddb.leveldb"
    db.mkdir(parents=True)
    for name, content in files.items():
        (db / name).write_text(content)
    return db


@pytest.mark.unit
def test_find_leveldbs_discovers_both_profiles(tmp_path):
    _make_leveldb(tmp_path, "WV2Profile_tfw", "teams.microsoft.com", {"CURRENT": "x"})
    _make_leveldb(tmp_path, "WV2Profile_tfl", "teams.live.com", {"CURRENT": "x"})
    found = locate_mod.find_leveldbs(tmp_path)
    assert len(found) == 2


@pytest.mark.unit
def test_work_and_personal_leveldb_selection(tmp_path):
    work = _make_leveldb(tmp_path, "WV2Profile_tfw", "teams.microsoft.com", {"CURRENT": "x"})
    personal = _make_leveldb(tmp_path, "WV2Profile_tfl", "teams.live.com", {"CURRENT": "x"})
    assert locate_mod.work_leveldb(tmp_path) == work
    assert locate_mod.personal_leveldb(tmp_path) == personal


@pytest.mark.unit
def test_locate_missing_base_returns_empty(tmp_path):
    assert locate_mod.find_leveldbs(tmp_path / "nope") == []
    assert locate_mod.work_leveldb(tmp_path / "nope") is None


@pytest.mark.unit
def test_snapshot_copies_and_drops_lock(tmp_path):
    src = _make_leveldb(
        tmp_path, "WV2Profile_tfw", "teams.microsoft.com",
        {"CURRENT": "c", "001.ldb": "data", "LOCK": "lock"},
    )
    dest = snapshot_leveldb(src, tmp_path / "snap")
    names = {p.name for p in dest.iterdir()}
    assert names == {"CURRENT", "001.ldb"}
    assert (dest / "001.ldb").read_text() == "data"


@pytest.mark.unit
def test_snapshot_overwrites_existing_dest(tmp_path):
    src = _make_leveldb(tmp_path, "WV2Profile_tfw", "teams.microsoft.com", {"CURRENT": "v2"})
    dest = tmp_path / "snap"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    snapshot_leveldb(src, dest)
    assert not (dest / "stale.txt").exists()


@pytest.mark.unit
def test_snapshot_missing_source_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        snapshot_leveldb(tmp_path / "missing", tmp_path / "snap")


@pytest.mark.unit
def test_dump_build_command():
    cmd = dump_mod.build_command(tmp := __import__("pathlib").Path("/snap"), binary="dfx")
    assert cmd == ["dfx", "db", "-s", "/snap", "-f", "chromium", "-o", "jsonl"]


@pytest.mark.unit
def test_dump_writes_runner_output(tmp_path):
    out = dump_mod.dump_to_jsonl(
        tmp_path / "snap",
        tmp_path / "out.jsonl",
        runner=lambda cmd: '{"ok": 1}\n',
    )
    assert out.read_text() == '{"ok": 1}\n'
