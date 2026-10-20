"""Command-line entry point: ingest | daemon | mcp | broker | wipe | locate."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import os
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


def _cmd_broker(args: argparse.Namespace) -> int:
    from teams_reader.broker import client, server

    profile = settings.active()
    return server.serve(profile.data_dir, client.socket_path(profile.data_dir), profile.db_path,
                        idle_after=args.idle_after)


def _cmd_mcp(_: argparse.Namespace) -> int:
    from teams_reader.mcp.server import main as mcp_main

    mcp_main()
    return 0


def _cmd_wipe(args: argparse.Namespace) -> int:
    if not args.yes:
        print("refusing to wipe without --yes")
        return 1
    from teams_reader.broker import control

    if control.stop(settings.active().data_dir):
        print("stopped the browser broker")
    db = config.db_path()
    for path in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        path.unlink(missing_ok=True)
    work_dir = settings.active().work_dir
    if work_dir.exists():
        shutil.rmtree(work_dir)
    print("wiped store and snapshots")
    others = control.mcp_server_pids()
    if others:
        print(f"MCP servers still running (pid {', '.join(map(str, others))}) keep the old store open; "
              "run /mcp in each Claude Code session (or restart it).")
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


def _uv_tool_dir() -> Path | None:
    try:
        out = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return Path(out.strip()).resolve()


def _snappy_flags() -> dict[str, str]:
    # A dependency pins python-snappy 0.6.1, which compiles against Homebrew's snappy.
    try:
        prefix = subprocess.run(["brew", "--prefix", "snappy"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return {}
    return {"CPPFLAGS": f"-I{prefix}/include", "LDFLAGS": f"-L{prefix}/lib"}


def _stop_brokers() -> None:
    """Stop every profile's broker so the next request starts one on the new code."""
    from teams_reader.broker import control

    names, _ = settings.list_profiles()
    for name in names:
        try:
            profile = settings.load_profile(name)
        except settings.ConfigError:
            continue
        if control.stop(profile.data_dir):
            print(f"Stopped the browser broker of profile {name!r}.")


def _cmd_update(_: argparse.Namespace) -> int:
    tools = _uv_tool_dir()
    if tools is None or not Path(sys.prefix).resolve().is_relative_to(tools):
        print(f"teams-reader runs from {sys.prefix}, not a uv tool install: update that checkout with "
              "`git pull` instead.", file=sys.stderr)
        return 1
    env = {**os.environ, **_snappy_flags()}
    print("Updating teams-reader from its source...")
    # The version string rarely changes, so plain `upgrade` reports "Nothing to upgrade"
    # without fetching the new commit; reinstalling the package does.
    code = subprocess.call(["uv", "tool", "upgrade", "--reinstall-package", "teams-reader", "teams-reader"], env=env)
    if code != 0:
        return code
    print("Updating its browser...")
    code = subprocess.call([_installed_exe(), "install-browser"])
    if code != 0:
        return code
    _stop_brokers()
    print("Updated; config, chats and sign-in are kept. In Claude Code run /mcp (or restart it) "
          "in each open session so the server uses the new version.")
    return 0


def _mcp_exists(name: str) -> bool:
    return subprocess.call(["claude", "mcp", "get", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0


def _register_mcp(cmd: list[str], ask=input, exists=_mcp_exists):
    from teams_reader.setup_wizard import StepResult

    name = cmd[cmd.index("--") - 1]
    if exists(name):
        from teams_reader.setup_wizard import Q_REPLACE

        if not ask(f"{Q_REPLACE} {name!r}. Replace it? [y/N]: ").strip().lower().startswith("y"):
            return StepResult(True, f"kept the existing {name!r}")
        subprocess.call(["claude", "mcp", "remove", "--scope", "user", name])
    code = subprocess.call(cmd)
    return StepResult(code == 0, f"registered as {name!r}" if code == 0 else f"`{' '.join(cmd)}` failed")


def _installed_exe() -> str:
    # Absolute path, so Claude Code finds it even without this shell's PATH.
    found = shutil.which("teams-reader")
    return str(Path(found).resolve()) if found else str(Path(sys.argv[0]).resolve())


def _setup_answers(args: argparse.Namespace):
    from teams_reader.setup_wizard import Answers

    return Answers(
        name=args.name, cache=args.cache, proxy=args.proxy, credentials_env=args.credentials_env,
        email_key=args.email_key, password_key=args.password_key, live_sync=not args.no_live_sync,
        make_default=args.make_default, sign_in=not args.no_sign_in, register=not args.no_register,
    )


def _cmd_setup(args: argparse.Namespace) -> int:
    from teams_reader import setup_steps, setup_wizard

    if args.yes:
        ask = setup_wizard.scripted(_setup_answers(args))
        print("teams-reader setup (unattended)\n")
    else:
        ask = input
        print("teams-reader setup: press Enter to accept the [default].\n")
    steps = setup_wizard.SetupSteps(
        read_cache=setup_steps.read_cache,
        connect=setup_steps.connect,
        register=lambda cmd: _register_mcp(cmd, ask=ask),
        claude_available=lambda: shutil.which("claude") is not None,
    )
    try:
        setup_wizard.run(ask=ask, say=print, steps=steps, exe=_installed_exe())
    except setup_wizard.SetupError as exc:
        print(f"teams-reader setup: {exc}", file=sys.stderr)
        return 2
    print("\nDone. Restart Claude Code (or run /mcp) and ask it about your Teams chats.")
    return 0


def _cmd_config(_: argparse.Namespace) -> int:
    print(settings.describe(settings.active()))
    print(f"{'config':>12}: {settings.resolve_config_path()} (override with TEAMS_READER_CONFIG)")
    return 0


def _cmd_profiles(_: argparse.Namespace) -> int:
    names, default = settings.list_profiles()
    for name in names:
        print(f"{'*' if name == default else ' '} {name}")
    return 0


def _cmd_use(args: argparse.Namespace) -> int:
    try:
        path = settings.set_default_profile(args.name)
    except settings.ConfigError as exc:
        print(f"teams-reader: {exc}", file=sys.stderr)
        return 1
    print(f"Default profile is now {args.name!r} ({path}).")
    print("Run /mcp in Claude Code (or restart it) so the teams-reader server switches.")
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    from teams_reader import logs

    path = logs.log_path()
    if not path.is_file():
        print(f"No log yet at {path}. It starts with the MCP server or a browser command.", file=sys.stderr)
        return 1
    print(f"==> {path}", file=sys.stderr, flush=True)
    tail = ["tail", "-n", str(args.lines)] + (["-F"] if args.follow else []) + [str(path)]
    os.execvp("tail", tail)
    return 0  # not reached


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="teams-reader")
    parser.add_argument("--profile", default=None, help="config profile to use (default: config default_profile)")
    sub = parser.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="first-run setup: config, sign-in, first sync, Claude Code")
    setup.add_argument("--yes", action="store_true", help="no questions: take the options below, defaults otherwise")
    setup.add_argument("--name", help="profile name (default: default)")
    setup.add_argument("--cache", help="work | personal | none | path (default: first found)")
    setup.add_argument("--proxy", help="proxy URL for Teams, e.g. socks5://127.0.0.1:1080")
    setup.add_argument("--credentials-env", help=".env file for automatic re-login")
    setup.add_argument("--email-key", help="email variable in that file (default EMAIL)")
    setup.add_argument("--password-key", help="password variable in that file (default PASSWORD)")
    setup.add_argument("--no-live-sync", action="store_true", help="no background browser sync")
    setup.add_argument("--make-default", action="store_true", help="make this the default profile")
    setup.add_argument("--no-sign-in", action="store_true", help="skip the Teams web sign-in and first sync")
    setup.add_argument("--no-register", action="store_true", help="don't register with Claude Code")
    setup.set_defaults(func=_cmd_setup)
    sub.add_parser("config", help="show the active profile's settings").set_defaults(func=_cmd_config)
    sub.add_parser("profiles", help="list profiles (* = default)").set_defaults(func=_cmd_profiles, profile_free=True)
    use = sub.add_parser("use", help="make a profile the default")
    use.add_argument("name")
    use.set_defaults(func=_cmd_use, profile_free=True)
    show_logs = sub.add_parser("logs", help="show the activity log (-f to follow it live)")
    show_logs.add_argument("-f", "--follow", action="store_true")
    show_logs.add_argument("-n", "--lines", type=int, default=40)
    show_logs.set_defaults(func=_cmd_logs, quiet=True)
    sub.add_parser("install-browser", help="download the Chromium build the browser features use").set_defaults(
        func=_cmd_install_browser
    )

    sub.add_parser("update", help="update teams-reader to the latest version (keeps config and data)").set_defaults(
        func=_cmd_update, quiet=True
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
    broker = sub.add_parser("broker", help="run the shared browser broker (MCP servers start it on demand)")
    broker.add_argument("--idle-after", type=float, default=1800.0, help="exit after this many idle seconds")
    broker.set_defaults(func=_cmd_broker)

    wipe = sub.add_parser("wipe", help="delete the store and snapshots")
    wipe.add_argument("--yes", action="store_true", help="confirm deletion")
    wipe.set_defaults(func=_cmd_wipe)

    sub.add_parser("locate", help="list Teams cache databases").set_defaults(func=_cmd_locate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "profile_free", False):
        try:
            settings.activate(args.profile)
        except settings.ConfigError as exc:
            parser.error(str(exc))
        if not getattr(args, "quiet", False):
            from teams_reader import logs

            logs.setup()
            logs.get("cli").info("teams-reader %s (profile %s)", args.command, settings.active().name)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
