"""Per-user configuration: named profiles from a TOML file, overridable by env.

``~/.config/teams-reader/config.toml`` (or ``TEAMS_READER_CONFIG``)::

    default_profile = "work"

    [profiles.work]
    cache = "work"                      # work | personal | none | /path/to/x.indexeddb.leveldb
    proxy = "socks5://127.0.0.1:1080"   # omit for a direct connection
    credentials_env = "~/path/.env"     # omit to never sign in automatically
    email_key = "EMAIL"
    password_key = "PASSWORD"
    live_sync = true                    # warm browser + background sync in the MCP server
    autologin = true
    data_dir = "~/somewhere"            # default: <app support>/profiles/<name>

Each profile has its own store, browser profile and images, so one MCP server
per profile (``teams-reader --profile NAME mcp``) keeps accounts apart. With no
config file the ``default`` profile uses the legacy data location, a direct
connection and no automatic sign-in. ``TEAMS_READER_*`` variables override the
selected profile.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

APP_SUPPORT = Path.home() / "Library/Application Support/teams-reader"
CONFIG_PATH = Path.home() / ".config/teams-reader/config.toml"
DEFAULT_PROFILE = "default"
_CACHE_KINDS = ("work", "personal", "none")
_OFF = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    data_dir: Path
    cache: str = "work"
    proxy: str | None = None
    credentials_env: Path | None = None
    email_key: str = "EMAIL"
    password_key: str = "PASSWORD"
    live_sync: bool = True
    autologin: bool = True
    db_override: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.db_override or self.data_dir / "store.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def browser_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)


def _path(value: str) -> Path:
    return Path(os.path.expanduser(value))


def _read_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _check_cache(value: str, where: str) -> str:
    if value in _CACHE_KINDS or value.startswith(("/", "~")):
        return os.path.expanduser(value) if value.startswith("~") else value
    raise ConfigError(f"{where}: cache must be one of {', '.join(_CACHE_KINDS)} or a path, got {value!r}")


def _from_table(name: str, table: Mapping, default_dir: Path) -> Profile:
    cache = _check_cache(str(table.get("cache", "work")), f"profile {name!r}")
    creds = table.get("credentials_env")
    return Profile(
        name=name,
        data_dir=_path(table["data_dir"]) if table.get("data_dir") else default_dir,
        cache=cache,
        proxy=table.get("proxy") or None,
        credentials_env=_path(creds) if creds else None,
        email_key=str(table.get("email_key", "EMAIL")),
        password_key=str(table.get("password_key", "PASSWORD")),
        live_sync=bool(table.get("live_sync", True)),
        autologin=bool(table.get("autologin", True)),
    )


def _apply_env(profile: Profile, environ: Mapping[str, str]) -> Profile:
    changes: dict = {}
    if "TEAMS_READER_PROXY" in environ:
        proxy = environ["TEAMS_READER_PROXY"].strip()
        changes["proxy"] = None if proxy.lower() in {"", "none", "off"} else proxy
    if environ.get("TEAMS_READER_DB"):
        changes["db_override"] = _path(environ["TEAMS_READER_DB"])
    if environ.get("TEAMS_READER_CACHE"):
        changes["cache"] = _check_cache(environ["TEAMS_READER_CACHE"], "TEAMS_READER_CACHE")
    if environ.get("TEAMS_READER_CREDENTIALS_ENV"):
        changes["credentials_env"] = _path(environ["TEAMS_READER_CREDENTIALS_ENV"])
    for key, field_name in (("TEAMS_READER_EMAIL_KEY", "email_key"), ("TEAMS_READER_PASSWORD_KEY", "password_key")):
        if environ.get(key):
            changes[field_name] = environ[key]
    for key, field_name in (("TEAMS_READER_LIVE", "live_sync"), ("TEAMS_READER_AUTOLOGIN", "autologin")):
        if key in environ:
            changes[field_name] = environ[key].strip().lower() not in _OFF
    return replace(profile, **changes) if changes else profile


def resolve_config_path(environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    return _path(environ.get("TEAMS_READER_CONFIG") or str(CONFIG_PATH))


def load_profile(
    name: str | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Profile:
    environ = os.environ if environ is None else environ
    path = config_path or resolve_config_path(environ)
    data = _read_config(path)
    profiles = data.get("profiles") or {}
    chosen = name or environ.get("TEAMS_READER_PROFILE") or data.get("default_profile") or DEFAULT_PROFILE
    if chosen in profiles:
        default_dir = APP_SUPPORT if chosen == DEFAULT_PROFILE else APP_SUPPORT / "profiles" / chosen
        profile = _from_table(chosen, profiles[chosen], default_dir)
    elif chosen == DEFAULT_PROFILE:
        profile = Profile(name=DEFAULT_PROFILE, data_dir=APP_SUPPORT)
    else:
        known = ", ".join(sorted(profiles)) or "none"
        raise ConfigError(f"unknown profile {chosen!r} in {path} (known: {known})")
    return _apply_env(profile, environ)


_active: dict[str, Profile | None] = {"profile": None, "name": None}


def activate(name: str | None) -> Profile:
    """Select the profile for this process (``None`` = config/env default)."""
    _active["name"] = name
    _active["profile"] = None
    return active()


def use(profile: Profile) -> Profile:
    """Pin an explicit profile for this process (scripts and tests)."""
    _active["profile"] = profile
    return profile


def active() -> Profile:
    if _active["profile"] is None:
        _active["profile"] = load_profile(_active["name"])
    return _active["profile"]


def describe(profile: Profile) -> str:
    """Human-readable summary of a profile (paths and switches, never secrets)."""
    rows = [
        ("profile", profile.name),
        ("data", str(profile.data_dir)),
        ("store", str(profile.db_path)),
        ("cache", profile.cache),
        ("proxy", profile.proxy or "direct"),
        ("credentials", str(profile.credentials_env) if profile.credentials_env else "none (no auto sign-in)"),
        ("live sync", "on" if profile.live_sync else "off"),
        ("auto sign-in", "on" if profile.autologin and profile.credentials_env else "off"),
    ]
    return "\n".join(f"{key:>12}: {value}" for key, value in rows)
