"""The real actions behind `teams-reader setup` (see setup_wizard.SetupSteps)."""
from __future__ import annotations

from typing import Callable

from teams_reader import settings
from teams_reader.setup_wizard import StepResult
from teams_reader.store import db as db_mod


def _profile_conn(name: str):
    profile = settings.activate(name)
    profile.ensure_dirs()
    return db_mod.connect(profile.db_path)


def read_cache(name: str, ingest_fn: Callable | None = None) -> StepResult:
    from teams_reader.ingest import ingest

    conn = _profile_conn(name)
    try:
        result = (ingest_fn or ingest)(conn)
    except FileNotFoundError as exc:
        return StepResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001 - reported in the summary
        return StepResult(False, f"could not read it: {exc}")
    finally:
        conn.close()
    return StepResult(True, f"{result.total_messages} messages, {result.chats_named} chat names")


def connect(name: str, backfill_fn: Callable | None = None) -> StepResult:
    """Sign in (visible window) and download every chat's history once."""
    from teams_reader.browser.profile_lock import ProfileInUse

    retry = f"retry with `teams-reader --profile {name} login`"
    if backfill_fn is None:
        from teams_reader.browser.backfill import backfill as backfill_fn

    def progress(chat: str, pages: int, count: int) -> None:
        print(f"    {chat}: {count} messages")

    conn = _profile_conn(name)
    try:
        summary = backfill_fn(conn, headless=False, full=True, on_progress=progress)
    except ProfileInUse:
        return StepResult(False, f"the teams-reader MCP server is using this profile; quit Claude Code, then {retry}")
    except Exception as exc:  # noqa: BLE001 - reported in the summary
        return StepResult(False, f"{str(exc)[:200]}; {retry}")
    finally:
        conn.close()
    return StepResult(
        True, f"signed in; {summary.chats_synced} chats, {summary.total_messages} messages stored"
    )
