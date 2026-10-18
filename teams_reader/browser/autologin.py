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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


SELECTORS = {
    "ms_email": "input[name=loginfmt]",
    "ms_password": "input[name=passwd]",
    "ms_next": "#idSIButton9",
    "ms_kmsi": "#KmsiCheckboxField, input[name=DontShowAgain]",
    "ms_error": "#usernameError, #passwordError",
    "pick_work": "#aadTile",  # "used with more than one account": Work or school
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
    if "pick_work" in present:
        return "pick_work"
    # Email first: the email page also carries an off-screen passwd input.
    if "ms_email" in present:
        return "email"
    if "ms_password" in present:
        return "password"
    if present & {"adfs_user", "adfs_password"}:
        return "adfs"
    if "ms_kmsi" in present:
        return "kmsi"
    return "waiting"


_ACTIONS = ("pick_work", "email", "password", "adfs", "kmsi")
_MAX_EMAIL_SUBMITS = 2
_CLICK_TIMEOUT_MS = 5_000


class AutoLogin:
    def __init__(
        self,
        creds: Credentials,
        on_mfa: Callable[[str | None], None],
        clock: Callable[[], float] = time.monotonic,
        cooldown: float = 6.0,
    ) -> None:
        self._creds = creds
        self._on_mfa = on_mfa
        self._clock = clock
        self._cooldown = cooldown
        self._mfa_reported = False
        self._submits: dict[str, int] = {}
        self._last: tuple[str, float] | None = None

    def step(self, page, present: set[str], read_text: Callable[[str], str]) -> str:
        """Act on the current login screen; return the detected state.

        After a submit the same screen is left alone for ``cooldown`` seconds
        while the page loads ("settling"); a screen that still does not advance
        stops the flow instead of resubmitting forever.
        """
        state = detect_state(page.url, present)
        if state == "error":
            raise LoginFailed(
                f"sign-in rejected: {read_text(SELECTORS['ms_error'])[:200]} "
                f"(email used: {email_shape(self._creds.email)})"
            )
        if state == "mfa":
            if not self._mfa_reported:
                self._mfa_reported = True
                number = read_text(SELECTORS["mfa_number"]) if "mfa_number" in present else None
                self._on_mfa(number)
            return state
        if state not in _ACTIONS:
            return state
        if self._last and self._last[0] == state and self._clock() - self._last[1] < self._cooldown:
            return "settling"
        self._guard(state, read_text)
        if self._act(page, state, present):
            self._submits[state] = self._submits.get(state, 0) + 1
            self._last = (state, self._clock())
        return state

    def _guard(self, state: str, read_text: Callable[[str], str]) -> None:
        sent = self._submits.get(state, 0)
        if state in ("password", "adfs") and sent:
            # Shown again after a submit: the password was rejected. Never retry it.
            raise LoginFailed("password screen shown again; not retrying")
        if state == "email" and sent >= _MAX_EMAIL_SUBMITS:
            reason = read_text(SELECTORS["ms_error"])[:200] or "the email screen did not advance"
            raise LoginFailed(f"the email was not accepted: {reason} ({email_shape(self._creds.email)})")

    def _act(self, page, state: str, present: set[str]) -> bool:
        if state == "email":
            self._fill_checked(page, SELECTORS["ms_email"], self._creds.email)
            return self._click(page, SELECTORS["ms_next"])
        if state == "password":
            page.fill(SELECTORS["ms_password"], self._creds.password)
            return self._click(page, SELECTORS["ms_next"])
        if state == "adfs":
            if "adfs_user" in present:
                page.fill(SELECTORS["adfs_user"], self._creds.email)
            page.fill(SELECTORS["adfs_password"], self._creds.password)
            return self._click(page, SELECTORS["adfs_submit"])
        if state == "pick_work":
            return self._click(page, SELECTORS["pick_work"])
        return self._click(page, SELECTORS["ms_next"])  # kmsi: "Stay signed in? Yes"

    @staticmethod
    def _click(page, selector: str) -> bool:
        # A busy/disabled button (page still loading) is retried on a later tick.
        try:
            page.click(selector, timeout=_CLICK_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            return False
        return True

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


def navigation_failure_message(url: str, failure: str) -> str:
    """Why a sign-in page could not load, naming the host (never the query)."""
    host = re.sub(r"^https?://([^/?#]+).*$", r"\1", url or "")
    if "CERT" in failure:
        hint = "its certificate is not trusted by the reader's browser"
    elif "SOCKS" in failure or "PROXY" in failure or "TUNNEL" in failure:
        hint = "the proxy could not reach it; check the VPN, or set the profile proxy"
    else:
        hint = "check the network/VPN"
    return f"{host} failed to load during sign-in ({failure}): {hint}"


def on_screen(box: dict | None) -> bool:
    """A bounding box the user could actually see (not moved off-screen)."""
    if not box or box["width"] <= 0 or box["height"] <= 0:
        return False
    return box["x"] + box["width"] > 0 and box["y"] + box["height"] > 0


def visible_keys(page) -> set[str]:
    """Which known login elements are visible on screen on a Playwright page."""
    keys = set()
    for key, selector in SELECTORS.items():
        try:
            for element in page.locator(selector).all():
                if element.is_visible() and on_screen(element.bounding_box()):
                    keys.add(key)
                    break
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
