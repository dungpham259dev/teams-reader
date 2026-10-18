import pytest

from teams_reader import settings
from teams_reader.browser import autologin


@pytest.mark.unit
def test_parse_env_handles_quotes_comments_and_export():
    text = '# c\nexport A=1\nB="two words"\nC=\'x=y\'\n\nbad line\n'
    assert autologin.parse_env(text) == {"A": "1", "B": "two words", "C": "x=y"}


@pytest.mark.unit
def test_load_credentials_and_secret_never_in_repr(tmp_path):
    env = tmp_path / ".env"
    env.write_text("JIRA_EMAIL=me@corp.example\nPASSWORD=s3cr3t-value\n")
    creds = autologin.load_credentials(env, email_key="JIRA_EMAIL")
    assert creds.email == "me@corp.example" and creds.password == "s3cr3t-value"
    assert "s3cr3t" not in repr(creds) and "s3cr3t" not in str(creds)


@pytest.mark.unit
def test_load_credentials_reports_missing_without_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("PASSWORD=s3cr3t-value\n")
    with pytest.raises(autologin.CredentialsError) as err:
        autologin.load_credentials(env)
    assert "EMAIL" in str(err.value) and "s3cr3t" not in str(err.value)
    assert autologin.load_credentials(tmp_path / "missing.env", required=False) is None


@pytest.mark.unit
def test_credentials_path_and_keys_are_configurable(tmp_path, monkeypatch):
    env = tmp_path / "x.env"
    env.write_text("MAIL=a@b.c\nPW=p\n")
    monkeypatch.setenv("TEAMS_READER_CREDENTIALS_ENV", str(env))
    monkeypatch.setenv("TEAMS_READER_EMAIL_KEY", "MAIL")
    monkeypatch.setenv("TEAMS_READER_PASSWORD_KEY", "PW")
    settings.activate(None)
    assert autologin.load_credentials().email == "a@b.c"


@pytest.mark.unit
@pytest.mark.parametrize("url,present,state", [
    ("https://login.microsoftonline.com/x", {"ms_email"}, "email"),
    ("https://login.microsoftonline.com/x", {"ms_password"}, "password"),
    ("https://adfs.corp.example/adfs/ls", {"adfs_user", "adfs_password"}, "adfs"),
    ("https://login.microsoftonline.com/x", {"ms_kmsi"}, "kmsi"),
    ("https://login.microsoftonline.com/x", {"mfa_number"}, "mfa"),
    ("https://login.microsoftonline.com/x", {"mfa_text"}, "mfa"),
    ("https://login.microsoftonline.com/x", {"ms_error"}, "error"),
    ("https://teams.microsoft.com/v2/", set(), "done"),
    ("https://login.microsoftonline.com/x", set(), "waiting"),
])
def test_detect_state(url, present, state):
    assert autologin.detect_state(url, present) == state


class _FakePage:
    """Walks the Microsoft flow: email -> password -> MFA number -> KMSI -> Teams."""

    def __init__(self, screens):
        self.screens = list(screens)
        self.filled = {}
        self.clicked = []

    @property
    def url(self):
        return self.screens[0][0]

    def present(self):
        return self.screens[0][1]

    def fill(self, selector, value):
        self.filled[selector] = value

    def click(self, selector):
        self.clicked.append(selector)
        self.screens.pop(0)

    def press(self, selector, key):
        self.click(selector)

    def text_of(self, selector):
        return "42"


def _drive(page, creds, steps=10):
    events = []
    login = autologin.AutoLogin(creds, on_mfa=events.append)
    for _ in range(steps):
        if login.step(page, page.present(), page.text_of) == "done":
            break
    return events


@pytest.mark.unit
def test_step_walks_microsoft_flow_and_reports_mfa_number_once():
    ms = "https://login.microsoftonline.com/x"
    page = _FakePage([
        (ms, {"ms_email"}), (ms, {"ms_password"}), (ms, {"mfa_number"}),
        (ms, {"ms_kmsi"}), ("https://teams.microsoft.com/v2/", set()),
    ])
    creds = autologin.Credentials("me@corp.example", "pw")

    # the MFA screen only advances when the user approves on the phone
    original = page.present
    calls = {"n": 0}

    def present():
        if "mfa_number" in original():
            calls["n"] += 1
            if calls["n"] == 3:
                page.screens.pop(0)
        return original()

    page.present = present
    events = _drive(page, creds)
    assert page.filled[autologin.SELECTORS["ms_email"]] == "me@corp.example"
    assert page.filled[autologin.SELECTORS["ms_password"]] == "pw"
    assert events == ["42"]  # surfaced once, not every tick
    assert page.url.startswith("https://teams.microsoft.com")


@pytest.mark.unit
def test_step_stops_on_login_error_instead_of_retrying_password():
    page = _FakePage([("https://login.microsoftonline.com/x", {"ms_error"})])
    login = autologin.AutoLogin(autologin.Credentials("a", "b"), on_mfa=lambda n: None)
    with pytest.raises(autologin.LoginFailed, match="incorrect"):
        login.step(page, {"ms_error"}, lambda s: "Your password is incorrect")


@pytest.mark.unit
def test_second_password_screen_is_not_retried():
    login = autologin.AutoLogin(autologin.Credentials("a", "b"), on_mfa=lambda n: None)
    page = _FakePage([("https://login.microsoftonline.com/x", {"ms_password"})] * 3)
    login.step(page, {"ms_password"}, page.text_of)
    with pytest.raises(autologin.LoginFailed, match="not retrying"):
        login.step(page, {"ms_password"}, page.text_of)


@pytest.mark.unit
@pytest.mark.parametrize("url,expected", [
    ("https://login.microsoftonline.com/common/oauth2/authorize", True),
    ("https://adfs.corp.example/adfs/ls/?wa=x", True),
    ("https://login.live.com/x", True),
    ("https://teams.microsoft.com/v2/", False),
    ("about:blank", False),
])
def test_on_login_page(url, expected):
    assert autologin.on_login_page(url) is expected


@pytest.mark.unit
def test_make_autologin_respects_disable_flag_and_missing_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("EMAIL=a@b.c\nPASSWORD=p\n")
    assert autologin.make_autologin() is None  # no credentials configured: never signs in
    monkeypatch.setenv("TEAMS_READER_CREDENTIALS_ENV", str(env))
    settings.activate(None)
    assert autologin.make_autologin() is not None
    monkeypatch.setenv("TEAMS_READER_AUTOLOGIN", "0")
    settings.activate(None)
    assert autologin.make_autologin() is None
    monkeypatch.delenv("TEAMS_READER_AUTOLOGIN")
    monkeypatch.setenv("TEAMS_READER_CREDENTIALS_ENV", str(tmp_path / "none.env"))
    settings.activate(None)
    assert autologin.make_autologin() is None
