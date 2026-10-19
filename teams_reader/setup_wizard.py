"""Interactive first-run setup: writes a config profile and registers the MCP server.

``run`` asks through injectable ``ask``/``say`` callables, so the flow is
testable; ``teams-reader setup`` wires them to the terminal. Credentials files
are only checked for the presence of their keys, never echoed.
"""
from __future__ import annotations

import re
import shutil
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from teams_reader import settings
from teams_reader.browser.autologin import parse_env
from teams_reader.cache import locate

Ask = Callable[[str], str]
Say = Callable[[str], None]

DEFAULT_PROXY = "socks5://127.0.0.1:1080"

# Question texts, shared by the terminal flow and scripted (unattended) answers.
Q_NAME = "Profile name"
Q_CACHE = "Local cache (work / personal / none / path)"
Q_USE_PROXY = "Does Teams need a proxy (e.g. a VPN SOCKS port)?"
Q_PROXY = "Proxy URL"
Q_AUTOLOGIN = "Sign in again automatically from a .env file when the session expires?"
Q_ENV = "Path to the .env file"
Q_EMAIL_KEY = "Email variable name"
Q_PASSWORD_KEY = "Password variable name"
Q_LIVE = "Keep a background browser syncing new messages while Claude uses it?"
Q_DEFAULT = "Make "
Q_SIGN_IN = "Sign in to Teams web now and download your chat history? (recommended)"
Q_REGISTER = "Register it with Claude Code now?"
Q_REPLACE = "Claude Code already has an MCP server"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BARE_KEY_RE = _NAME_RE


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else _toml_value(key)


def render_config(data: dict) -> str:
    lines = [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in data.items() if k != "profiles"]
    for name, table in (data.get("profiles") or {}).items():
        lines += ["", f"[profiles.{_toml_key(name)}]"]
        lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in table.items()]
    return "\n".join(lines).lstrip("\n") + "\n"


def merge_profile(existing: dict, name: str, table: dict, make_default: bool) -> dict:
    profiles = {**(existing.get("profiles") or {}), name: table}
    merged = {k: v for k, v in existing.items() if k != "profiles"}
    if make_default:
        merged["default_profile"] = name
    merged["profiles"] = profiles
    return merged


def check_credentials(path: Path, email_key: str, password_key: str) -> str | None:
    """None when the dotenv file has both keys; otherwise what is wrong (no values)."""
    if not path.is_file():
        return f"file not found: {path}"
    values = parse_env(path.read_text(encoding="utf-8"))
    missing = [key for key in (email_key, password_key) if not values.get(key)]
    return f"{path} has no value for: {', '.join(missing)}" if missing else None


def proxy_problem(proxy: str) -> str | None:
    parts = urlsplit(proxy)
    if parts.scheme not in ("socks5", "socks4", "http", "https") or not parts.hostname or not parts.port:
        return "expected scheme://host:port, e.g. socks5://127.0.0.1:1080"
    return None


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def available_caches() -> list[str]:
    found = []
    if locate.work_leveldb():
        found.append("work")
    if locate.personal_leveldb():
        found.append("personal")
    return found


def mcp_add_command(name: str, exe: str, is_default: bool) -> list[str]:
    server = "teams-reader" if is_default else f"teams-{name}"
    args = [exe, "mcp"] if is_default else [exe, "--profile", name, "mcp"]
    return ["claude", "mcp", "add", "--scope", "user", server, "--", *args]


def _ask_until(ask: Ask, say: Say, prompt: str, default: str, problem: Callable[[str], str | None]) -> str:
    while True:
        answer = ask(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip() or default
        issue = problem(answer)
        if issue is None:
            return answer
        say(f"  ! {issue}")


def _yes(ask: Ask, prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = ask(f"{prompt} [{hint}]: ").strip().lower()
    return default if not answer else answer.startswith("y")


def _cache_problem(value: str) -> str | None:
    try:
        settings._check_cache(value, "cache")
    except settings.ConfigError as exc:
        return str(exc)
    return None


def _ask_profile(ask: Ask, say: Say, existing: dict) -> tuple[str, dict]:
    known = existing.get("profiles") or {}
    name = _ask_until(
        ask, say, Q_NAME, "default" if not known else "",
        lambda v: None if _NAME_RE.match(v) else "use letters, digits, - or _",
    )
    caches = available_caches()
    say(f"Teams caches on this Mac: {', '.join(caches) or 'none found'}")
    cache = _ask_until(ask, say, Q_CACHE, caches[0] if caches else "none",
                       _cache_problem)
    table: dict = {"cache": cache}

    if _yes(ask, Q_USE_PROXY, False):
        proxy = _ask_until(ask, say, Q_PROXY, DEFAULT_PROXY, proxy_problem)
        parts = urlsplit(proxy)
        if not port_open(parts.hostname, parts.port):
            say(f"  ! nothing is listening on {parts.hostname}:{parts.port} right now; start the VPN before syncing")
        table["proxy"] = proxy

    autologin = False
    if _yes(ask, Q_AUTOLOGIN, False):
        while True:
            path = Path(ask(f"{Q_ENV}: ").strip()).expanduser()
            email_key = ask(f"{Q_EMAIL_KEY} [EMAIL]: ").strip() or "EMAIL"
            password_key = ask(f"{Q_PASSWORD_KEY} [PASSWORD]: ").strip() or "PASSWORD"
            issue = check_credentials(path, email_key, password_key)
            if issue is None:
                break
            say(f"  ! {issue}")
        table.update(credentials_env=str(path), email_key=email_key, password_key=password_key)
        autologin = True

    table["live_sync"] = _yes(ask, Q_LIVE, True)
    table["autologin"] = autologin
    return name, table


class SetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class Answers:
    """Answers for an unattended setup (``teams-reader setup --yes``); None = default."""

    name: str | None = None
    cache: str | None = None
    proxy: str | None = None
    credentials_env: str | None = None
    email_key: str | None = None
    password_key: str | None = None
    live_sync: bool = True
    make_default: bool = False
    sign_in: bool = True
    register: bool = True


def _yn(flag: bool) -> str:
    return "y" if flag else "n"


def scripted(answers: Answers) -> Ask:
    """An ``ask`` that answers from ``answers``; a rejected value raises instead of re-asking."""
    table = [
        (Q_NAME, "--name", answers.name or ""),
        (Q_CACHE, "--cache", answers.cache or ""),
        (Q_USE_PROXY, "--proxy", _yn(bool(answers.proxy))),
        (Q_PROXY, "--proxy", answers.proxy or ""),
        (Q_AUTOLOGIN, "--credentials-env", _yn(bool(answers.credentials_env))),
        (Q_ENV, "--credentials-env", answers.credentials_env or ""),
        (Q_EMAIL_KEY, "--email-key", answers.email_key or ""),
        (Q_PASSWORD_KEY, "--password-key", answers.password_key or ""),
        (Q_LIVE, "--no-live-sync", _yn(answers.live_sync)),
        (Q_DEFAULT, "--make-default", _yn(answers.make_default)),
        (Q_SIGN_IN, "--no-sign-in", _yn(answers.sign_in)),
        (Q_REGISTER, "--no-register", _yn(answers.register)),
        (Q_REPLACE, "", "y"),
    ]
    asked: set[str] = set()

    def ask(prompt: str) -> str:
        for question, flag, answer in table:
            if prompt.startswith(question):
                if question in asked and question not in (Q_REPLACE,):
                    raise SetupError(f"{flag} was not accepted (see the message above)")
                asked.add(question)
                return answer
        raise SetupError(f"unexpected question in unattended setup: {prompt!r}")

    return ask


@dataclass(frozen=True)
class StepResult:
    ok: bool
    detail: str


@dataclass(frozen=True)
class SetupSteps:
    """The actions that finish a setup, injectable so the flow is testable."""

    read_cache: Callable[[str], StepResult]
    connect: Callable[[str], StepResult]  # sign in to Teams web + first full sync
    register: Callable[[list[str]], StepResult]
    claude_available: Callable[[], bool]


def _finish(ask: Ask, say: Say, steps: SetupSteps, name: str, table: dict, cmd: list[str]) -> None:
    results: list[tuple[str, StepResult]] = []
    if table["cache"] != "none":
        say("\nReading the Teams desktop cache...")
        results.append(("Local cache", steps.read_cache(name)))

    say("\nSyncing from Teams web gives real chat names, full history, images and links,")
    say("and lets Claude see new messages within ~30s. It opens a browser window to sign in.")
    if _yes(ask, Q_SIGN_IN, True):
        say("A browser window opens; sign in if asked (approve MFA on your phone).")
        say("It goes blank while history downloads and closes by itself.")
        results.append(("Teams web", steps.connect(name)))
    else:
        results.append(("Teams web", StepResult(False, f"skipped; run `teams-reader --profile {name} login` later")))

    if not steps.claude_available():
        results.append(("Claude Code", StepResult(False, f"CLI not found; later run: {' '.join(cmd)}")))
    elif _yes(ask, Q_REGISTER, True):
        results.append(("Claude Code", steps.register(cmd)))
    else:
        results.append(("Claude Code", StepResult(False, f"skipped; later run: {' '.join(cmd)}")))

    say("\nSetup summary:")
    for label, result in results:
        say(f"  {'✓' if result.ok else '✗'} {label}: {result.detail}")


def run(
    ask: Ask,
    say: Say,
    config_path: Path | None = None,
    steps: SetupSteps | None = None,
    exe: str | None = None,
) -> settings.Profile:
    """Ask for one profile and save it; with ``steps``, also read the cache, sign
    in and sync from Teams web, and register with Claude Code, so later use
    needs no manual step."""
    path = config_path or settings.resolve_config_path()
    existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    name, table = _ask_profile(ask, say, existing)

    current_default = existing.get("default_profile")
    make_default = not current_default or current_default == name or _yes(
        ask, f"{Q_DEFAULT}{name!r} the default profile (currently {current_default!r})?", False
    )
    if path.is_file():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(merge_profile(existing, name, table, make_default)), encoding="utf-8")
    profile = settings.load_profile(name, config_path=path, environ={})
    say(f"\nSaved {path}:\n{settings.describe(profile)}")

    if steps is not None:
        _finish(ask, say, steps, name, table, mcp_add_command(name, exe or "teams-reader", is_default=make_default))
    return profile
