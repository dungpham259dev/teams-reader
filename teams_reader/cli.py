"""Command-line entry point: ingest | daemon | mcp | wipe | locate."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from teams_reader import config, settings
from teams_reader.cache import locate as locate_mod
from teams_reader.daemon import poll, wait_for_new_message
from teams_reader.ingest import ingest
from teams_reader.store import db as db_mod


def _cmd_ingest(_: argparse.Namespace) -> int:
    config.ensure_app_dirs()
    conn = db_mod.connect(config.db_path())
    try:
        result = ingest(conn)
    finally:
        conn.close()
    print(
        f"ingested: +{result.new_messages} new, {result.total_messages} total, "
        f"{result.chats_named} chats named"
    )
    return 0


def _cmd_daemon(args: argparse.Namespace) -> int:
    config.ensure_app_dirs()
    conn = db_mod.connect(config.db_path())
    print(f"polling every {args.interval}s (Ctrl+C to stop)...")
    poll(conn, interval=args.interval, on_result=_print_result)
    return 0


def _print_result(result) -> None:
    if result.total_messages < 0:
        print("tick failed (skipped)")
    elif result.new_messages:
        print(f"+{result.new_messages} new ({result.total_messages} total)")


def _cmd_verify_live(args: argparse.Namespace) -> int:
    config.ensure_app_dirs()
    conn = db_mod.connect(config.db_path())
    print(f"baselining cache, then watching for a new message (up to {args.timeout}s)...")
    print("-> now send yourself a message in Teams.")
    try:
        ok = wait_for_new_message(conn, timeout=args.timeout, interval=args.interval)
    finally:
        conn.close()
    if ok:
        print("PASS: a new message was picked up from the cache.")
        return 0
    print("TIMEOUT: no new message appeared (Teams not syncing, or none sent).")
    return 1


def _cmd_login(args: argparse.Namespace) -> int:
    from teams_reader.browser import session

    print("opening Teams web (log in + let a chat load); the window closes on success...")
    ok = session.login(timeout_ms=int(args.timeout * 1000))
    print("login OK — session saved." if ok else "login did not complete in time.")
    return 0 if ok else 1


def _cmd_backfill(args: argparse.Namespace) -> int:
    config.ensure_app_dirs()
    from teams_reader.browser import backfill as backfill_mod

    conn = db_mod.connect(config.db_path())

    def _progress(name: str, pages: int, count: int) -> None:
        print(f"  {name}: {count} messages over {pages} page(s)")

    try:
        summary = backfill_mod.backfill(
            conn,
            max_pages=args.max_pages,
            chat_filter=args.chat,
            headless=not args.headful,
            full=args.full,
            on_progress=_progress,
        )
    finally:
        conn.close()
    print(
        f"backfill: {summary.chats_synced} chats, +{summary.new_messages} new, "
        f"{summary.total_messages} total"
    )
    return 0


def _cmd_open_link(args: argparse.Namespace) -> int:
    from teams_reader.browser import links as links_mod

    print("A browser window opens; sign in if asked, then close the window.")
    links_mod.open_link(args.url)
    return 0


def _cmd_fetch_image(args: argparse.Namespace) -> int:
    config.ensure_app_dirs()
    from teams_reader.browser import images as images_mod

    conn = db_mod.connect(config.db_path())
    try:
        result = images_mod.download_images(
            conn,
            chat=args.chat,
            msg_ids=[args.msg_id] if args.msg_id else None,
            headless=not args.headful,
            limit=args.limit,
        )
    finally:
        conn.close()
    print(f"images: {result['downloaded']}/{result['requested']} downloaded")
    return 0


def _cmd_mcp(_: argparse.Namespace) -> int:
    from teams_reader.mcp.server import main as mcp_main

    mcp_main()
    return 0


def _cmd_wipe(args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to wipe without --yes")
        return 1
    db = config.db_path()
    if db.exists():
        db.unlink()
    work_dir = settings.active().work_dir
    if work_dir.exists():
        shutil.rmtree(work_dir)
    print("wiped store and snapshots")
    return 0


def _cmd_locate(_: argparse.Namespace) -> int:
    dbs = locate_mod.find_leveldbs()
    if not dbs:
        print("no Teams IndexedDB databases found")
        return 1
    mine = locate_mod.active_leveldb()
    for db in dbs:
        tag = f"  <- profile {settings.active().name!r}" if db == mine else ""
        print(f"{db}{tag}")
    return 0


def _cmd_install_browser(_: argparse.Namespace) -> int:
    # The Chromium build must match this environment's Playwright version.
    return subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])


def _mcp_exists(name: str) -> bool:
    return subprocess.call(["claude", "mcp", "get", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def _register_mcp(cmd: list[str], ask=input, exists=_mcp_exists):
    from teams_reader.setup_wizard import StepResult

    name = cmd[cmd.index("--") - 1]
    if exists(name):
        if not ask(f"Claude Code already has an MCP server {name!r}. Replace it? [y/N]: ").strip().lower().startswith("y"):
            return StepResult(True, f"kept the existing {name!r}")
        subprocess.call(["claude", "mcp", "remove", "--scope", "user", name])
    code = subprocess.call(cmd)
    return StepResult(code == 0, f"registered as {name!r}" if code == 0 else f"`{' '.join(cmd)}` failed")


def _installed_exe() -> str:
    # Absolute path, so Claude Code finds it even without this shell's PATH.
    found = shutil.which("teams-reader")
    return str(Path(found).resolve()) if found else str(Path(sys.argv[0]).resolve())


def _cmd_setup(_: argparse.Namespace) -> int:
    from teams_reader import setup_steps, setup_wizard

    print("teams-reader setup: press Enter to accept the [default].\n")
    steps = setup_wizard.SetupSteps(
        read_cache=setup_steps.read_cache,
        connect=setup_steps.connect,
        register=_register_mcp,
        claude_available=lambda: shutil.which("claude") is not None,
    )
    setup_wizard.run(ask=input, say=print, steps=steps, exe=_installed_exe())
    print("\nDone. Restart Claude Code (or run /mcp) and ask it about your Teams chats.")
    return 0


def _cmd_config(_: argparse.Namespace) -> int:
    print(settings.describe(settings.active()))
    print(f"{'config':>12}: {settings.resolve_config_path()} (override with TEAMS_READER_CONFIG)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teams-reader")
    parser.add_argument("--profile", default=None, help="config profile to use (default: config default_profile)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="interactive first-run setup (config + Claude Code)").set_defaults(func=_cmd_setup)
    sub.add_parser("config", help="show the active profile's settings").set_defaults(func=_cmd_config)
    sub.add_parser("install-browser", help="download the Chromium build the browser features use").set_defaults(
        func=_cmd_install_browser
    )

    sub.add_parser("ingest", help="read the cache into the store").set_defaults(func=_cmd_ingest)

    daemon = sub.add_parser("daemon", help="poll the cache on an interval")
    daemon.add_argument("--interval", type=float, default=45.0)
    daemon.set_defaults(func=_cmd_daemon)

    verify = sub.add_parser("verify-live", help="watch for a new live message in the cache")
    verify.add_argument("--timeout", type=float, default=120.0)
    verify.add_argument("--interval", type=float, default=5.0)
    verify.set_defaults(func=_cmd_verify_live)

    login = sub.add_parser("login", help="log into Teams web once (browser backfill)")
    login.add_argument("--timeout", type=float, default=300.0)
    login.set_defaults(func=_cmd_login)

    backfill = sub.add_parser("backfill", help="sync new/changed chats via the browser session")
    backfill.add_argument("--max-pages", type=int, default=50, help="pages per chat")
    backfill.add_argument("--chat", default=None, help="only this chat (name or id)")
    backfill.add_argument("--headful", action="store_true", help="show the browser window (slower; may throttle)")
    backfill.add_argument("--full", action="store_true", help="refetch every chat, not only changed ones")
    backfill.set_defaults(func=_cmd_backfill)

    open_link = sub.add_parser("open-link", help="open a link in the reader browser (one-time site login)")
    open_link.add_argument("url")
    open_link.set_defaults(func=_cmd_open_link)

    fetch_image = sub.add_parser("fetch-image", help="download chat images via the browser session")
    fetch_image.add_argument("--chat", default=None, help="only this chat (name or id)")
    fetch_image.add_argument("--msg-id", default=None, help="only this message's image")
    fetch_image.add_argument("--limit", type=int, default=50, help="max images per run")
    fetch_image.add_argument("--headful", action="store_true", help="show the browser window")
    fetch_image.set_defaults(func=_cmd_fetch_image)

    sub.add_parser("mcp", help="run the MCP server (stdio)").set_defaults(func=_cmd_mcp)

    wipe = sub.add_parser("wipe", help="delete the store and snapshots")
    wipe.add_argument("--yes", action="store_true", help="confirm deletion")
    wipe.set_defaults(func=_cmd_wipe)

    sub.add_parser("locate", help="list Teams cache databases").set_defaults(func=_cmd_locate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings.activate(args.profile)
    except settings.ConfigError as exc:
        parser.error(str(exc))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
