"""Tell apart the Teams accounts that share one desktop cache.

The desktop app keeps every work account in the same LevelDB; each IndexedDB
database is named ``Teams:<manager>:react-web-client:<tenant>:<user>:<locale>``.
When more than one account has messages there, a profile reads only its own:
the ``account`` set in the profile, or the one whose chats match the chats its
Teams web session synced. Until that is known nothing is ingested, so chats
of different accounts never mix.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from teams_reader import logs
from teams_reader.parse.decode import unwrap
from teams_reader.parse.messages import extract_messages
from teams_reader.store import db as db_mod

ACCOUNT_STATE = "cache_account"
PENDING_STATE = "cache_account_pending"
_MESSAGE_DB = "Teams:replychain-manager:"
_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_DB_RE = re.compile(rf"react-web-client:({_GUID}):(?:8:orgid:)?({_GUID}):", re.IGNORECASE)


_log = logs.get("accounts")


class MixedCacheError(RuntimeError):
    pass


@dataclass(frozen=True)
class CacheAccount:
    user: str
    tenant: str
    chat_ids: frozenset[str]


def _unsigned(value: int) -> int:
    # dfindexeddb reads Chromium's minimal little-endian id as signed (154 -> -102).
    return value + (1 << (8 * (((~value).bit_length() + 8) // 8))) if value < 0 else value


def database_names(records: Iterable[Any]) -> dict[int, str]:
    """database_id -> name, from the cache's ``DatabaseNameKey`` records."""
    names = {}
    for record in records:
        key = record.get("key") if isinstance(record, dict) else None
        if isinstance(key, dict) and key.get("__type__") == "DatabaseNameKey" and isinstance(record.get("value"), int):
            names[_unsigned(record["value"])] = key.get("database_name") or ""
    return names


def _db_name(record: Any, names: dict[int, str]) -> str:
    if not isinstance(record, dict):
        return ""
    return record.get("database_name") or names.get(record.get("database_id"), "")


def user_of(database_name: str) -> str | None:
    match = _DB_RE.search(database_name or "")
    return match.group(2).lower() if match else None


def message_accounts(records: list[Any]) -> list[CacheAccount]:
    """Accounts that own a message database, with the chats cached for each."""
    names = database_names(records)
    grouped: dict[tuple[str, str], list[Any]] = {}
    for record in records:
        name = _db_name(record, names)
        match = _DB_RE.search(name) if name.startswith(_MESSAGE_DB) else None
        if match and "value" in record:
            key = (match.group(2).lower(), match.group(1).lower())
            grouped.setdefault(key, []).append(unwrap(record["value"]))
    found = []
    for (user, tenant), values in grouped.items():
        chats = frozenset(m.chat_id for m in extract_messages(values) if m.chat_id)
        if chats:
            found.append(CacheAccount(user, tenant, chats))
    return sorted(found, key=lambda a: a.user)


def records_for(records: list[Any], user: str) -> list[Any]:
    names = database_names(records)
    user = user.lower()
    return [r for r in records if user in _db_name(r, names).lower()]


def match_account(found: list[CacheAccount], web_chat_ids: set[str]) -> str | None:
    """The one account whose cached chats overlap the web session's chats most."""
    scores = sorted(((len(a.chat_ids & web_chat_ids), a.user) for a in found), reverse=True)
    if not scores or scores[0][0] == 0 or (len(scores) > 1 and scores[0][0] == scores[1][0]):
        return None
    return scores[0][1]


def _mixed_message(found: list[CacheAccount]) -> str:
    rows = "; ".join(f"{a.user} ({len(a.chat_ids)} chats, tenant {a.tenant})" for a in found)
    return (
        f"The Teams desktop cache holds {len(found)} Teams accounts: {rows}. "
        "Sign in to Teams web with this profile (`teams-reader login`) so teams-reader "
        'can tell which is yours, or set account = "<user id>" in the profile.'
    )


def resolve(conn: sqlite3.Connection, records: list[Any], configured: str | None) -> str | None:
    """The account to keep records for, or None to keep everything (one account)."""
    if configured:
        db_mod.delete_state(conn, PENDING_STATE)
        return configured.lower()
    found = message_accounts(records)
    if len(found) <= 1:
        db_mod.delete_state(conn, PENDING_STATE)
        return None
    users = {a.user for a in found}
    remembered = db_mod.get_state(conn, ACCOUNT_STATE)
    if remembered in users:
        db_mod.delete_state(conn, PENDING_STATE)
        _log.debug("cache shared by %d accounts; reading %s (remembered)", len(found), remembered)
        return remembered
    matched = match_account(found, db_mod.web_chat_ids(conn))
    if matched:
        _log.info("cache shared by %d accounts; matched %s by its web chats", len(found), matched)
        db_mod.set_state(conn, ACCOUNT_STATE, matched)
        db_mod.delete_state(conn, PENDING_STATE)
        return matched
    message = _mixed_message(found)
    db_mod.set_state(conn, PENDING_STATE, message)
    raise MixedCacheError(message)
