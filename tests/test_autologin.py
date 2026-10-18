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

    def click(self, selector, **kwargs):
        self.clicked.append(selector)
        self.screens.pop(0)

    def press(self, selector, key, **kwargs):
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
    clock = _Clock()
    login = _login(clock)
    page = _FakePage([("https://login.microsoftonline.com/x", {"ms_password"})] * 3)
    login.step(page, {"ms_password"}, page.text_of)
    clock.now = 10.0
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


@pytest.mark.unit
def test_parse_env_drops_inline_comments_but_keeps_hashes_in_values():
    text = 'A=a@b.c  # jira login\nB="pa#ss"  # quoted\nC=x#y\nD=  spaced value  \n'
    assert autologin.parse_env(text) == {"A": "a@b.c", "B": "pa#ss", "C": "x#y", "D": "spaced value"}


@pytest.mark.unit
@pytest.mark.parametrize("email,shape", [
    ("john.doe@corp.example", "21 chars, ***@corp.example"),
    ("thann008", "8 chars, no @"),
    ("", "empty"),
])
def test_email_shape_hides_the_local_part(email, shape):
    assert autologin.email_shape(email) == shape


class _ClearingPage(_FakePage):
    """The login page wipes the first value typed (its JS was not ready yet)."""

    def __init__(self, screens):
        super().__init__(screens)
        self.values = {}
        self.wipes = 1

    def fill(self, selector, value):
        super().fill(selector, value)
        self.values[selector] = "" if self.wipes else value
        self.wipes = max(0, self.wipes - 1)

    def input_value(self, selector):
        return self.values.get(selector, "")


@pytest.mark.unit
def test_email_is_refilled_when_the_page_cleared_it():
    ms = "https://login.microsoftonline.com/x"
    page = _ClearingPage([(ms, {"ms_email"}), (ms, {"ms_password"})])
    login = autologin.AutoLogin(autologin.Credentials("me@corp.example", "pw"), on_mfa=lambda n: None)
    login.step(page, {"ms_email"}, page.text_of)
    assert page.values[autologin.SELECTORS["ms_email"]] == "me@corp.example"
    assert page.clicked == [autologin.SELECTORS["ms_next"]]


@pytest.mark.unit
def test_rejected_email_error_shows_only_its_shape():
    page = _FakePage([("https://login.microsoftonline.com/x", {"ms_error"})])
    login = autologin.AutoLogin(autologin.Credentials("secret.name@corp.example", "pw"), on_mfa=lambda n: None)
    with pytest.raises(autologin.LoginFailed) as err:
        login.step(page, {"ms_error"}, lambda s: "Enter a valid email address")
    assert "***@corp.example" in str(err.value) and "secret.name" not in str(err.value)


@pytest.mark.unit
def test_email_screen_wins_over_a_prefilled_hidden_password_field():
    # Microsoft's email page also carries an off-screen passwd input.
    state = autologin.detect_state("https://login.microsoftonline.com/x", {"ms_email", "ms_password", "ms_next"})
    assert state == "email"


@pytest.mark.unit
@pytest.mark.parametrize("box,shown", [
    ({"x": 10, "y": 20, "width": 300, "height": 30}, True),
    ({"x": -9999, "y": 20, "width": 300, "height": 30}, False),
    ({"x": 10, "y": 20, "width": 0, "height": 0}, False),
    (None, False),
])
def test_on_screen(box, shown):
    assert autologin.on_screen(box) is shown


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _StuckPage(_FakePage):
    """A screen that does not advance when clicked (e.g. still loading)."""

    def click(self, selector, **kwargs):
        self.clicked.append(selector)


def _login(clock):
    return autologin.AutoLogin(autologin.Credentials("me@corp.example", "pw"), on_mfa=lambda n: None,
                               clock=clock, cooldown=5.0)


@pytest.mark.unit
def test_same_screen_is_not_resubmitted_while_it_is_loading():
    clock = _Clock()
    page = _StuckPage([("https://login.microsoftonline.com/x", {"ms_email"})])
    login = _login(clock)
    login.step(page, {"ms_email"}, page.text_of)
    clock.now = 2.0
    assert login.step(page, {"ms_email"}, page.text_of) == "settling"
    assert len(page.clicked) == 1


@pytest.mark.unit
def test_email_screen_that_never_advances_stops_with_the_page_error():
    clock = _Clock()
    page = _StuckPage([("https://login.microsoftonline.com/x", {"ms_email"})])
    login = _login(clock)
    login.step(page, {"ms_email"}, page.text_of)
    clock.now = 10.0
    login.step(page, {"ms_email"}, page.text_of)
    clock.now = 20.0
    with pytest.raises(autologin.LoginFailed, match="not accepted"):
        login.step(page, {"ms_email"}, lambda s: "We couldn't find an account with that username.")
    assert len(page.clicked) == 2


@pytest.mark.unit
def test_a_busy_button_is_retried_later_instead_of_crashing():
    clock = _Clock()

    class _BusyPage(_StuckPage):
        busy = True

        def click(self, selector, **kwargs):
            if self.busy:
                raise TimeoutError("element is not enabled")
            super().click(selector)

    page = _BusyPage([("https://login.microsoftonline.com/x", {"ms_email"})])
    login = _login(clock)
    assert login.step(page, {"ms_email"}, page.text_of) == "email"
    page.busy = False
    clock.now = 1.0  # a failed click starts no cooldown
    login.step(page, {"ms_email"}, page.text_of)
    assert page.clicked == [autologin.SELECTORS["ms_next"]]


@pytest.mark.unit
def test_account_type_picker_chooses_work_or_school():
    ms = "https://login.microsoftonline.com/x"
    assert autologin.detect_state(ms, {"pick_work"}) == "pick_work"
    page = _FakePage([(ms, {"pick_work"}), (ms, {"ms_password"})])
    login = autologin.AutoLogin(autologin.Credentials("a@b.c", "pw"), on_mfa=lambda n: None)
    assert login.step(page, {"pick_work"}, page.text_of) == "pick_work"
    assert page.clicked == [autologin.SELECTORS["pick_work"]]


@pytest.mark.unit
def test_failed_sign_in_navigation_is_explained():
    message = autologin.navigation_failure_message(
        "https://sts.corp.example/Trust/2005/UsernameMixed?x=1", "net::ERR_SOCKS_CONNECTION_FAILED")
    assert "sts.corp.example" in message and "ERR_SOCKS_CONNECTION_FAILED" in message
    assert "proxy" in message and "x=1" not in message
    cert = autologin.navigation_failure_message("https://idp.corp.example/", "net::ERR_CERT_AUTHORITY_INVALID")
    assert "certificate" in cert
