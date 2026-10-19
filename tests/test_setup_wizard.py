import tomllib

import pytest

from teams_reader import setup_wizard


@pytest.mark.unit
def test_render_round_trips_through_tomllib():
    data = {
        "default_profile": "work",
        "profiles": {
            "work": {"cache": "work", "proxy": 'socks5://h:1080', "live_sync": True},
            "odd \"name\"": {"cache": "none", "autologin": False, "data_dir": "~/a b\\c"},
        },
    }
    assert tomllib.loads(setup_wizard.render_config(data)) == data


@pytest.mark.unit
def test_merge_profile_keeps_others_and_sets_default():
    existing = {"default_profile": "a", "profiles": {"a": {"cache": "work"}}}
    merged = setup_wizard.merge_profile(existing, "b", {"cache": "none"}, make_default=True)
    assert merged == {"default_profile": "b", "profiles": {"a": {"cache": "work"}, "b": {"cache": "none"}}}
    assert existing["default_profile"] == "a"  # input untouched


@pytest.mark.unit
def test_check_credentials_reports_keys_without_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("EMAIL=me@x.y\nPASSWORD=hunter2\n")
    assert setup_wizard.check_credentials(env, "EMAIL", "PASSWORD") is None
    problem = setup_wizard.check_credentials(env, "MAIL", "PASSWORD")
    assert "MAIL" in problem and "hunter2" not in problem and "me@x.y" not in problem
    assert "not found" in setup_wizard.check_credentials(tmp_path / "x.env", "EMAIL", "PASSWORD")


@pytest.mark.unit
@pytest.mark.parametrize("proxy,ok", [("socks5://127.0.0.1:1080", True), ("http://h:8080", True),
                                      ("127.0.0.1:1080", False), ("socks5://h", False)])
def test_proxy_format(proxy, ok):
    assert (setup_wizard.proxy_problem(proxy) is None) is ok


@pytest.mark.unit
def test_mcp_command_uses_profile_only_when_needed():
    exe = "/u/bin/teams-reader"
    assert setup_wizard.mcp_add_command("default", exe, is_default=True) == [
        "claude", "mcp", "add", "--scope", "user", "teams-reader", "--", exe, "mcp"]
    assert setup_wizard.mcp_add_command("client-b", exe, is_default=False) == [
        "claude", "mcp", "add", "--scope", "user", "teams-client-b", "--", exe, "--profile", "client-b", "mcp"]


class _Answers:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


@pytest.mark.unit
def test_wizard_writes_a_profile_from_answers(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    env = tmp_path / "creds.env"
    env.write_text("JIRA_EMAIL=a@b.c\nPASSWORD=p\n")
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["work", "personal"])
    monkeypatch.setattr(setup_wizard, "port_open", lambda host, port: True)
    ask = _Answers(
        "acme",                       # profile name
        "work",                       # cache
        "y", "",                      # proxy? -> default socks5://127.0.0.1:1080
        "y", str(env), "JIRA_EMAIL", "",  # auto sign-in, file, email key, password key (default)
        "",                           # live sync (default yes)
    )
    out = []
    profile = setup_wizard.run(ask=ask, say=out.append, config_path=cfg)
    data = tomllib.loads(cfg.read_text())
    assert data["default_profile"] == "acme"
    assert data["profiles"]["acme"] == {
        "cache": "work", "proxy": "socks5://127.0.0.1:1080", "credentials_env": str(env),
        "email_key": "JIRA_EMAIL", "password_key": "PASSWORD", "live_sync": True, "autologin": True,
    }
    assert profile.name == "acme" and profile.proxy == "socks5://127.0.0.1:1080"
    assert not any("p\n" in line for line in out)


@pytest.mark.unit
def test_wizard_reasks_invalid_answers_and_backs_up_old_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('default_profile = "old"\n[profiles.old]\ncache = "work"\n')
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: [])
    ask = _Answers(
        "bad name!", "new",           # invalid profile name, then valid
        "everything", "none",         # invalid cache, then valid
        "n",                          # no proxy
        "n",                          # no auto sign-in
        "n",                          # no live sync
        "n",                          # don't make it the default
    )
    setup_wizard.run(ask=ask, say=lambda s: None, config_path=cfg)
    data = tomllib.loads(cfg.read_text())
    assert data["default_profile"] == "old"
    assert data["profiles"]["new"] == {"cache": "none", "live_sync": False, "autologin": False}
    assert (tmp_path / "config.toml.bak").read_text().startswith('default_profile = "old"')


def _steps(calls, connect_ok=True, claude=True):
    def read_cache(name):
        calls.append(("cache", name))
        return setup_wizard.StepResult(True, "586 messages in 12 chats")

    def connect(name):
        calls.append(("connect", name))
        return setup_wizard.StepResult(connect_ok, "15 chats, 666 messages" if connect_ok else "VPN down")

    def register(cmd):
        calls.append(("register", cmd[5]))
        return setup_wizard.StepResult(True, "registered")

    return setup_wizard.SetupSteps(read_cache=read_cache, connect=connect, register=register,
                                   claude_available=lambda: claude)


@pytest.mark.unit
def test_setup_finishes_everything_and_prints_a_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["work"])
    calls, out = [], []
    ask = _Answers("", "", "n", "n", "", "", "")  # defaults; then: sign in [Y], register [Y]
    setup_wizard.run(ask=ask, say=out.append, config_path=tmp_path / "c.toml", steps=_steps(calls))
    assert calls == [("cache", "default"), ("connect", "default"), ("register", "teams-reader")]
    summary = "\n".join(out)
    assert "✓ Local cache: 586 messages in 12 chats" in summary
    assert "✓ Teams web: 15 chats, 666 messages" in summary
    assert "✓ Claude Code: registered" in summary


@pytest.mark.unit
def test_skipped_or_failed_steps_say_how_to_finish_later(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: [])
    calls, out = [], []
    ask = _Answers("work2", "none", "n", "n", "", "n")  # cache none; decline sign-in; no claude CLI
    setup_wizard.run(ask=ask, say=out.append, config_path=tmp_path / "c.toml",
                     steps=_steps(calls, claude=False))
    summary = "\n".join(out)
    assert calls == []  # no cache to read, sign-in declined, no Claude CLI
    assert "✗ Teams web: skipped" in summary and "teams-reader --profile work2 login" in summary
    assert "✗ Claude Code: CLI not found" in summary and "claude mcp add" in summary


@pytest.mark.unit
def test_failed_sign_in_is_reported_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["work"])
    calls, out = [], []
    ask = _Answers("", "", "n", "n", "", "", "")
    setup_wizard.run(ask=ask, say=out.append, config_path=tmp_path / "c.toml",
                     steps=_steps(calls, connect_ok=False))
    assert "✗ Teams web: VPN down" in "\n".join(out)
    assert ("register", "teams-reader") in calls


@pytest.mark.unit
def test_unattended_setup_takes_every_answer_from_options(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["work"])
    monkeypatch.setattr(setup_wizard, "port_open", lambda host, port: True)
    env = tmp_path / "creds.env"
    env.write_text("JIRA_EMAIL=a@b.c\nPASSWORD=p\n")
    answers = setup_wizard.Answers(
        name="acme", proxy="socks5://127.0.0.1:1080", credentials_env=str(env), email_key="JIRA_EMAIL",
        sign_in=False,
    )
    calls, out = [], []
    setup_wizard.run(ask=setup_wizard.scripted(answers), say=out.append, config_path=tmp_path / "c.toml",
                     steps=_steps(calls))
    data = tomllib.loads((tmp_path / "c.toml").read_text())
    assert data["profiles"]["acme"]["proxy"] == "socks5://127.0.0.1:1080"
    assert data["profiles"]["acme"]["email_key"] == "JIRA_EMAIL" and data["profiles"]["acme"]["autologin"]
    assert calls == [("cache", "acme"), ("register", "teams-reader")]


@pytest.mark.unit
def test_unattended_setup_fails_loudly_on_a_bad_option(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["work"])
    ask = setup_wizard.scripted(setup_wizard.Answers(cache="nonsense"))
    with pytest.raises(setup_wizard.SetupError, match="--cache"):
        setup_wizard.run(ask=ask, say=lambda s: None, config_path=tmp_path / "c.toml")
    assert not (tmp_path / "c.toml").exists()


@pytest.mark.unit
def test_unattended_setup_keeps_defaults_when_options_are_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_wizard, "available_caches", lambda: ["personal"])
    profile = setup_wizard.run(ask=setup_wizard.scripted(setup_wizard.Answers()), say=lambda s: None,
                               config_path=tmp_path / "c.toml")
    assert profile.name == "default" and profile.cache == "personal" and profile.proxy is None
    assert profile.live_sync and not profile.autologin
