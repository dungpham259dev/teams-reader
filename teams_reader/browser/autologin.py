"""Unattended Microsoft sign-in for the reader browser.

When the Teams session has expired, the login pages are filled from a dotenv
file named by the profile's ``credentials_env`` (keys ``email_key``/``password_key``). Each
:meth:`AutoLogin.step` performs at most one action, so it slots into the capture
loop. MFA cannot be automated: its number/prompt is reported via ``on_mfa`` and
the flow waits for approval on the phone. A rejected password stops the flow
(no retries, to avoid locking the account). Secrets are never logged.
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


SELECTORS = {
    "ms_email": "input[name=loginfmt]",
    "ms_password": "input[name=passwd]",
    "ms_next": "#idSIButton9",
    "ms_kmsi": "#KmsiCheckboxField, input[name=DontShowAgain]",
    "ms_error": "#usernameError, #passwordError",
    "mfa_number": "#idRichContext_DisplaySign",
    "mfa_text": "#idDiv_SAOTCAS_Title, #idDiv_SAOTCC_Title",
    "adfs_user": "#userNameInput",
    "adfs_password": "#passwordInput",
    "adfs_submit": "#submitButton",
}
_DONE_HOSTS = ("teams.microsoft.com", "teams.cloud.microsoft")
_LOGIN_RE = re.compile(r"^https?://(login\.microsoftonline\.com|login\.live\.com|[^/]*adfs[^/]*|[^/]+/adfs/)")
_LINE_RE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class CredentialsError(RuntimeError):
    pass


class LoginFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str = field(repr=False)


_QUOTED_RE = re.compile(r"""^(["'])(.*?)\1(?:\s+#.*)?$""")


def _env_value(raw: str) -> str:
    value = raw.strip()
    quoted = _QUOTED_RE.match(value)
    if quoted:
        return quoted.group(2)
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()  # "x  # comment" -> "x"


def email_shape(email: str) -> str:
    """Describe a sign-in name for diagnostics without revealing who it is."""
    if not email:
        return "empty"
    if "@" not in email:
        return f"{len(email)} chars, no @"
    return f"{len(email)} chars, ***@{email.rsplit('@', 1)[1]}"


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        out[match.group(1)] = _env_value(match.group(2))
    return out


def load_credentials(
    path: Path | None = None,
    required: bool = True,
    email_key: str | None = None,
    password_key: str | None = None,
) -> Credentials | None:
    """Read the sign-in email/password from a dotenv file (default: the active profile's)."""
    from teams_reader import settings

    profile = settings.active()
    env_path = Path(path) if path else profile.credentials_env
    email_key = email_key or profile.email_key
    password_key = password_key or profile.password_key
    if env_path is None:
        if required:
            raise CredentialsError(f"profile {profile.name!r} has no credentials_env")
        return None
    if not env_path.is_file():
        if required:
            raise CredentialsError(f"credentials file not found: {env_path}")
        return None
    values = parse_env(env_path.read_text(encoding="utf-8"))
    missing = [key for key in (email_key, password_key) if not values.get(key)]
    if missing:
        raise CredentialsError(f"{env_path} lacks: {', '.join(missing)}")
    return Credentials(values[email_key], values[password_key])


def on_login_page(url: str) -> bool:
    return bool(_LOGIN_RE.match(url or ""))


def notify_mfa(number: str | None) -> None:
    """Tell the user to approve sign-in on the phone (stderr + macOS notification)."""
    message = f"Teams reader sign-in: enter {number} in Authenticator" if number else (
        "Teams reader sign-in: approve the request on your phone"
    )
    print(message, file=sys.stderr, flush=True)
    script = f'display notification "{message}" with title "teams-reader" sound name "Glass"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5, capture_output=True)
    except Exception:  # noqa: BLE001 - the stderr line is enough
        pass


def make_autologin() -> "AutoLogin | None":
    """An AutoLogin from the configured credentials, or None when disabled/unavailable."""
    from teams_reader import settings

    if not settings.active().autologin:
        return None
    try:
        creds = load_credentials(required=False)
    except CredentialsError:
        return None
    return AutoLogin(creds, on_mfa=notify_mfa) if creds else None


def detect_state(url: str, present: set[str]) -> str:
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url or "")
    if host.endswith(_DONE_HOSTS):
        return "done"
    if "ms_error" in present:
        return "error"
    if present & {"mfa_number", "mfa_text"}:
        return "mfa"
    if "ms_password" in present:
        return "password"
    if "ms_email" in present:
        return "email"
    if present & {"adfs_user", "adfs_password"}:
        return "adfs"
    if "ms_kmsi" in present:
        return "kmsi"
    return "waiting"


class AutoLogin:
    def __init__(self, creds: Credentials, on_mfa: Callable[[str | None], None]) -> None:
        self._creds = creds
        self._on_mfa = on_mfa
        self._mfa_reported = False
        self._password_sent = False

    def step(self, page, present: set[str], read_text: Callable[[str], str]) -> str:
        """Act on the current login screen; return the detected state."""
        state = detect_state(page.url, present)
        if state == "error":
            raise LoginFailed(
                f"sign-in rejected: {read_text(SELECTORS['ms_error'])[:200]} "
                f"(email used: {email_shape(self._creds.email)})"
            )
        if state == "mfa" and not self._mfa_reported:
            self._mfa_reported = True
            number = read_text(SELECTORS["mfa_number"]) if "mfa_number" in present else None
            self._on_mfa(number)
        elif state == "email":
            self._fill_checked(page, SELECTORS["ms_email"], self._creds.email)
            page.click(SELECTORS["ms_next"])
        elif state == "password":
            self._submit_password_once()
            page.fill(SELECTORS["ms_password"], self._creds.password)
            page.click(SELECTORS["ms_next"])
        elif state == "adfs":
            self._submit_password_once()
            if "adfs_user" in present:
                page.fill(SELECTORS["adfs_user"], self._creds.email)
            page.fill(SELECTORS["adfs_password"], self._creds.password)
            page.click(SELECTORS["adfs_submit"])
        elif state == "kmsi":
            page.click(SELECTORS["ms_next"])  # "Stay signed in? Yes"
        return state

    @staticmethod
    def _fill_checked(page, selector: str, value: str, attempts: int = 3) -> None:
        # The sign-in page can wipe a value typed before its scripts are ready.
        for _ in range(attempts):
            page.fill(selector, value)
            reader = getattr(page, "input_value", None)
            if reader is None or reader(selector) == value:
                return
            if hasattr(page, "wait_for_timeout"):
                page.wait_for_timeout(500)
        raise LoginFailed(f"the sign-in page kept clearing the email field ({email_shape(value)})")

    def _submit_password_once(self) -> None:
        # A second password screen means the first was rejected: stop, don't lock the account.
        if self._password_sent:
            raise LoginFailed("password screen shown again; not retrying")
        self._password_sent = True


def visible_keys(page) -> set[str]:
    """Which known login elements are visible on a Playwright page."""
    keys = set()
    for key, selector in SELECTORS.items():
        try:
            if page.locator(selector).first.is_visible():
                keys.add(key)
        except Exception:  # noqa: BLE001 - mid-navigation pages throw
            continue
    return keys


def read_text(page) -> Callable[[str], str]:
    def _read(selector: str) -> str:
        try:
            return page.locator(selector).first.inner_text(timeout=2_000).strip()
        except Exception:  # noqa: BLE001
            return ""
    return _read
