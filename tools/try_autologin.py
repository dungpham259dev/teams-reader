"""Try unattended sign-in in a throwaway, visible browser profile.

The real reader profile is untouched. Expect an MFA prompt on your phone; the
number (if any) shows as a macOS notification. Run it yourself:

    venv/bin/python tools/try_autologin.py            # default profile
    TEAMS_READER_PROFILE=work venv/bin/python tools/try_autologin.py
"""
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from teams_reader import settings
from teams_reader.browser import live

# Same proxy/credentials as the active profile, but a throwaway data dir.
settings.use(replace(settings.active(), data_dir=Path(tempfile.mkdtemp(prefix="teams-reader-try-"))))
start = time.perf_counter()
try:
    browser_session = live.PlaywrightSession(interactive=True, headless=False)
except Exception as exc:  # noqa: BLE001 - report the outcome
    print(f"FAILED after {time.perf_counter() - start:.0f}s: {type(exc).__name__}: {exc}")
else:
    print(f"Signed in after {time.perf_counter() - start:.0f}s; chatsvc captured.")
    browser_session.close()
