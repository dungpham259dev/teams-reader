"""Filesystem locations for teams-reader (macOS).

Per-profile paths (store, work dir, browser profile) live in
:mod:`teams_reader.settings`; this module keeps the Teams app's own locations.
"""
from __future__ import annotations

from pathlib import Path

from teams_reader import settings

HOME = Path.home()

# New Teams (v2) sandboxed container and its two WebView profiles.
TEAMS_EBWEBVIEW = (
    HOME
    / "Library/Containers/com.microsoft.teams2/Data/Library"
    / "Application Support/Microsoft/MSTeams/EBWebView"
)
PROFILE_WORK = "WV2Profile_tfw"      # teams.microsoft.com (work / school / guest)
PROFILE_PERSONAL = "WV2Profile_tfl"  # teams.live.com (consumer)

APP_SUPPORT = settings.APP_SUPPORT


def db_path() -> Path:
    return settings.active().db_path


def ensure_app_dirs() -> None:
    settings.active().ensure_dirs()
