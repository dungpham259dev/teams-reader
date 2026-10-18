from pathlib import Path

import pytest

from teams_reader import settings

CONFIG = """
default_profile = "acme"

[profiles.acme]
cache = "work"
proxy = "socks5://127.0.0.1:1080"
credentials_env = "~/secrets/acme.env"
email_key = "JIRA_EMAIL"
password_key = "PASSWORD"

[profiles.side]
cache = "none"
live_sync = false
data_dir = "/tmp/side-data"
"""


@pytest.fixture()
def cfg(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    return path


@pytest.mark.unit
def test_no_config_gives_safe_default_profile(tmp_path):
    p = settings.load_profile(config_path=tmp_path / "missing.toml", environ={})
    assert p.name == "default"
    assert p.proxy is None and p.credentials_env is None
    assert p.cache == "work" and p.live_sync is True and p.autologin is True
    assert p.data_dir == settings.APP_SUPPORT  # legacy location, existing data kept


@pytest.mark.unit
def test_default_profile_from_config(cfg):
    p = settings.load_profile(config_path=cfg, environ={})
    assert p.name == "acme"
    assert p.proxy == "socks5://127.0.0.1:1080"
    assert p.credentials_env == Path.home() / "secrets/acme.env"
    assert p.data_dir == settings.APP_SUPPORT / "profiles" / "acme"
    assert p.db_path == p.data_dir / "store.db"
    assert p.browser_dir == p.data_dir / "browser-profile"


@pytest.mark.unit
def test_named_profile_and_env_selection(cfg):
    by_arg = settings.load_profile("side", config_path=cfg, environ={})
    by_env = settings.load_profile(config_path=cfg, environ={"TEAMS_READER_PROFILE": "side"})
    for p in (by_arg, by_env):
        assert p.name == "side" and p.cache == "none" and p.live_sync is False
        assert p.proxy is None and p.data_dir == Path("/tmp/side-data")


@pytest.mark.unit
def test_env_overrides_config(cfg):
    p = settings.load_profile(config_path=cfg, environ={
        "TEAMS_READER_PROXY": "none",
        "TEAMS_READER_DB": "/tmp/x.db",
        "TEAMS_READER_LIVE": "0",
        "TEAMS_READER_AUTOLOGIN": "0",
        "TEAMS_READER_CACHE": "personal",
    })
    assert p.proxy is None and p.db_path == Path("/tmp/x.db")
    assert p.live_sync is False and p.autologin is False and p.cache == "personal"


@pytest.mark.unit
def test_unknown_profile_and_bad_values_fail_clearly(cfg, tmp_path):
    with pytest.raises(settings.ConfigError, match="nope.*acme, side"):
        settings.load_profile("nope", config_path=cfg, environ={})
    bad = tmp_path / "bad.toml"
    bad.write_text('[profiles.x]\ncache = "everything"\n')
    with pytest.raises(settings.ConfigError, match="cache"):
        settings.load_profile("x", config_path=bad, environ={})
    broken = tmp_path / "broken.toml"
    broken.write_text("[profiles.x\n")
    with pytest.raises(settings.ConfigError, match="broken.toml"):
        settings.load_profile(config_path=broken, environ={})


@pytest.mark.unit
def test_cache_may_be_an_explicit_leveldb_path(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[profiles.x]\ncache = "/some/where.indexeddb.leveldb"\n')
    assert settings.load_profile("x", config_path=path, environ={}).cache == "/some/where.indexeddb.leveldb"


@pytest.mark.unit
def test_activate_switches_the_active_profile(cfg, monkeypatch):
    monkeypatch.setenv("TEAMS_READER_CONFIG", str(cfg))
    settings.activate("side")
    assert settings.active().name == "side"
    settings.activate(None)
    assert settings.active().name == "acme"


@pytest.mark.unit
def test_describe_never_includes_secrets(cfg):
    text = settings.describe(settings.load_profile(config_path=cfg, environ={}))
    assert "acme" in text and "socks5" in text and "acme.env" in text


@pytest.mark.unit
def test_package_ships_the_schema():
    from importlib.resources import files

    assert files("teams_reader").joinpath("store/schema.sql").is_file()
