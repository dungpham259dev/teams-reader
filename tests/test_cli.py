import logging

import pytest

from teams_reader import cli


@pytest.mark.unit
def test_no_command_exits():
    with pytest.raises(SystemExit):
        cli.main([])


@pytest.mark.unit
def test_wipe_requires_yes(capsys):
    rc = cli.main(["wipe"])
    assert rc == 1
    assert "refusing" in capsys.readouterr().out


@pytest.mark.unit
def test_wipe_deletes_db_and_work(tmp_path, monkeypatch, capsys):
    db = tmp_path / "store.db"
    db.write_text("data")
    work = tmp_path / "work"
    work.mkdir()
    (work / "snapshot").mkdir()
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[profiles.default]\ndata_dir = "{tmp_path}"\n')
    monkeypatch.setenv("TEAMS_READER_CONFIG", str(cfg))

    rc = cli.main(["wipe", "--yes"])
    assert rc == 0
    assert not db.exists()
    assert not work.exists()
    assert "wiped" in capsys.readouterr().out


@pytest.mark.unit
def test_locate_reports_when_empty(monkeypatch, capsys):
    monkeypatch.setattr(cli.locate_mod, "find_leveldbs", lambda: [])
    rc = cli.main(["locate"])
    assert rc == 1
    assert "no Teams" in capsys.readouterr().out


@pytest.mark.unit
def test_locate_marks_work_db(monkeypatch, capsys, tmp_path):
    from pathlib import Path

    work = Path("/x/WV2Profile_tfw/https_teams.microsoft.com_0.indexeddb.leveldb")
    other = Path("/x/WV2Profile_tfl/https_teams.live.com_0.indexeddb.leveldb")
    monkeypatch.setattr(cli.locate_mod, "find_leveldbs", lambda: [work, other])
    monkeypatch.setattr(cli.locate_mod, "active_leveldb", lambda: work)
    rc = cli.main(["locate"])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"{work}  <- profile 'default'" in out


@pytest.mark.unit
def test_ingest_dispatch(monkeypatch, tmp_path, capsys):
    from teams_reader.ingest import IngestResult

    monkeypatch.setattr(cli.config, "ensure_app_dirs", lambda: None)
    monkeypatch.setattr(cli.config, "db_path", lambda: ":memory:")
    monkeypatch.setattr(cli, "ingest", lambda conn: IngestResult(3, 3, 1, 42))
    rc = cli.main(["ingest"])
    assert rc == 0
    assert "+3 new" in capsys.readouterr().out


@pytest.mark.unit
def test_daemon_dispatch(monkeypatch, capsys):
    captured = {}

    def fake_poll(conn, interval, on_result):
        captured["interval"] = interval

    monkeypatch.setattr(cli.config, "ensure_app_dirs", lambda: None)
    monkeypatch.setattr(cli.config, "db_path", lambda: ":memory:")
    monkeypatch.setattr(cli, "poll", fake_poll)
    rc = cli.main(["daemon", "--interval", "5"])
    assert rc == 0
    assert captured["interval"] == 5.0


@pytest.mark.unit
@pytest.mark.parametrize("found,rc", [(True, 0), (False, 1)])
def test_verify_live_dispatch(monkeypatch, capsys, found, rc):
    monkeypatch.setattr(cli.config, "ensure_app_dirs", lambda: None)
    monkeypatch.setattr(cli.config, "db_path", lambda: ":memory:")
    monkeypatch.setattr(cli, "wait_for_new_message", lambda conn, timeout, interval: found)
    assert cli.main(["verify-live", "--timeout", "10"]) == rc
    out = capsys.readouterr().out
    assert ("PASS" in out) == found


@pytest.mark.unit
def test_mcp_dispatch(monkeypatch):
    import teams_reader.mcp.server as server_mod

    called = {"n": 0}
    monkeypatch.setattr(server_mod, "main", lambda: called.__setitem__("n", 1))
    rc = cli.main(["mcp"])
    assert rc == 0
    assert called["n"] == 1


@pytest.mark.unit
def test_print_result_variants(capsys):
    from teams_reader.ingest import IngestResult

    cli._print_result(IngestResult(0, -1, 0, 0))
    cli._print_result(IngestResult(2, 10, 0, 5))
    cli._print_result(IngestResult(0, 10, 0, 5))
    out = capsys.readouterr().out
    assert "tick failed" in out
    assert "+2 new" in out


@pytest.mark.unit
def test_login_dispatch(monkeypatch, capsys):
    from teams_reader.browser import session

    monkeypatch.setattr(session, "login", lambda timeout_ms: True)
    rc = cli.main(["login", "--timeout", "1"])
    assert rc == 0
    assert "login OK" in capsys.readouterr().out


@pytest.mark.unit
def test_login_dispatch_failure(monkeypatch, capsys):
    from teams_reader.browser import session

    monkeypatch.setattr(session, "login", lambda timeout_ms: False)
    rc = cli.main(["login"])
    assert rc == 1


@pytest.mark.unit
def test_backfill_dispatch(monkeypatch, tmp_path, capsys):
    from teams_reader.browser import backfill as backfill_mod

    monkeypatch.setattr(cli.config, "db_path", lambda: tmp_path / "store.db")
    monkeypatch.setattr(cli.config, "ensure_app_dirs", lambda: None)

    summary = backfill_mod.BackfillSummary(chats_synced=2, new_messages=10, total_messages=42)

    def fake_backfill(conn, **kwargs):
        assert kwargs["max_pages"] == 7
        assert kwargs["chat_filter"] == "Sigma"
        return summary

    monkeypatch.setattr(backfill_mod, "backfill", fake_backfill)
    rc = cli.main(["backfill", "--max-pages", "7", "--chat", "Sigma"])
    assert rc == 0
    assert "2 chats, +10 new, 42 total" in capsys.readouterr().out


@pytest.mark.unit
def test_fetch_image_dispatch(monkeypatch, tmp_path, capsys):
    from teams_reader.browser import images as images_mod

    monkeypatch.setattr(cli.config, "db_path", lambda: tmp_path / "store.db")
    monkeypatch.setattr(cli.config, "ensure_app_dirs", lambda: None)

    def fake_download(conn, **kwargs):
        assert kwargs["chat"] == "Sigma"
        assert kwargs["headless"] is True
        return {"requested": 3, "downloaded": 2}

    monkeypatch.setattr(images_mod, "download_images", fake_download)
    rc = cli.main(["fetch-image", "--chat", "Sigma"])
    assert rc == 0
    assert "2/3 downloaded" in capsys.readouterr().out


@pytest.mark.unit
def test_install_browser_uses_the_same_interpreter(monkeypatch):
    import sys

    calls = []
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd: calls.append(cmd) or 0)
    assert cli.main(["install-browser"]) == 0
    assert calls == [[sys.executable, "-m", "playwright", "install", "chromium"]]


@pytest.mark.unit
def test_config_command_prints_profile(capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "profile: default" in out and "proxy: direct" in out


@pytest.mark.unit
def test_register_replaces_an_existing_server_only_when_confirmed(monkeypatch):
    ran = []
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, **kw: ran.append(cmd) or 0)
    cmd = ["claude", "mcp", "add", "--scope", "user", "teams-reader", "--", "/x/teams-reader", "mcp"]

    cli._register_mcp(cmd, ask=lambda p: "n", exists=lambda name: True)
    assert ran == []  # kept the existing registration

    cli._register_mcp(cmd, ask=lambda p: "y", exists=lambda name: True)
    assert ran == [["claude", "mcp", "remove", "--scope", "user", "teams-reader"], cmd]

    ran.clear()
    cli._register_mcp(cmd, ask=lambda p: "", exists=lambda name: False)
    assert ran == [cmd]


@pytest.fixture()
def two_profiles(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(f'default_profile = "a"\n\n[profiles.a]\ncache = "none"\ndata_dir = "{tmp_path}/a"\n\n[profiles.b]\ncache = "none"\n')
    monkeypatch.setenv("TEAMS_READER_CONFIG", str(path))
    return path


@pytest.mark.unit
def test_profiles_lists_and_marks_default(two_profiles, capsys):
    assert cli.main(["profiles"]) == 0
    out = capsys.readouterr().out
    assert "* a" in out and "  b" in out


@pytest.mark.unit
def test_use_switches_the_default(two_profiles, capsys):
    assert cli.main(["use", "b"]) == 0
    assert 'default_profile = "b"' in two_profiles.read_text()
    assert "/mcp" in capsys.readouterr().out


@pytest.mark.unit
def test_use_repairs_a_config_whose_default_is_gone(two_profiles):
    two_profiles.write_text(two_profiles.read_text().replace('"a"\n', '"gone"\n', 1))
    assert cli.main(["use", "a"]) == 0


@pytest.mark.unit
def test_use_unknown_profile_fails(two_profiles, capsys):
    assert cli.main(["use", "zzz"]) == 1
    assert "known: a, b" in capsys.readouterr().err


@pytest.mark.unit
def test_commands_write_the_activity_log(two_profiles, capsys):
    from teams_reader import logs

    cli.main(["config"])
    path = logs.log_path()
    for handler in logging.getLogger("teams_reader").handlers:
        handler.flush()
    assert "teams-reader config (profile a)" in path.read_text()


@pytest.mark.unit
def test_logs_without_a_log_explains(two_profiles, capsys):
    assert cli.main(["logs"]) == 1
    assert "No log yet" in capsys.readouterr().err


@pytest.mark.unit
def test_setup_yes_runs_without_questions(tmp_path, monkeypatch, capsys):
    from teams_reader import setup_steps, setup_wizard

    cfg = tmp_path / "config.toml"
    monkeypatch.setenv("TEAMS_READER_CONFIG", str(cfg))
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["work"])
    monkeypatch.setattr(setup_steps, "read_cache", lambda name: setup_wizard.StepResult(True, "ok"))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail(f"asked: {prompt}"))
    rc = cli.main(["setup", "--yes", "--name", "acme", "--cache", "none", "--no-sign-in", "--no-register"])
    assert rc == 0
    assert "[profiles.acme]" in cfg.read_text()
    assert "skipped; run `teams-reader --profile acme login`" in capsys.readouterr().out


@pytest.mark.unit
def test_setup_yes_with_a_bad_option_exits_2(tmp_path, monkeypatch, capsys):
    from teams_reader import setup_wizard

    monkeypatch.setenv("TEAMS_READER_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["work"])
    assert cli.main(["setup", "--yes", "--proxy", "not-a-url", "--no-sign-in", "--no-register"]) == 2
    assert "--proxy was not accepted" in capsys.readouterr().err


@pytest.mark.unit
def test_update_upgrades_the_uv_tool_then_the_browser(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "_uv_tool_dir", lambda: cli.Path(cli.sys.prefix).parent)
    monkeypatch.setattr(cli, "_snappy_flags", lambda: {"CPPFLAGS": "-Ix"})
    monkeypatch.setattr(cli.subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw.get("env", {}).get("CPPFLAGS"))) or 0)
    assert cli.main(["update"]) == 0
    assert calls[0] == (["uv", "tool", "upgrade", "--reinstall-package", "teams-reader", "teams-reader"], "-Ix")
    assert calls[1][0][-1] == "install-browser"
    assert "/mcp" in capsys.readouterr().out


@pytest.mark.unit
def test_update_from_a_source_checkout_says_git_pull(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_uv_tool_dir", lambda: cli.Path("/somewhere/else"))
    monkeypatch.setattr(cli.subprocess, "call", lambda *a, **k: pytest.fail("must not run"))
    assert cli.main(["update"]) == 1
    assert "git pull" in capsys.readouterr().err
