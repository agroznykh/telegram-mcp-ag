import asyncio
import os
from types import SimpleNamespace

import pytest
from telethon import errors

from telegram_mcp_ag import config, terminal_login

TELEGRAM_ENV_PREFIX = "TELEGRAM_"


@pytest.fixture(autouse=True)
def clean_telegram_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(TELEGRAM_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class ScriptedQR:
    """A QR login whose wait() plays back ``outcomes``: an exception class to
    raise, or anything else to return as the logged-in user."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.url = "tg://login?token=Zm9vYmFy"
        self.recreated = 0

    async def wait(self, timeout=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            raise outcome(request=None) if issubclass(outcome, errors.RPCError) else outcome()
        return outcome

    async def recreate(self):
        self.recreated += 1


class FakeClient:
    def __init__(self, qr_outcomes=(), passwords_ok=(True,), codes_ok=(True,), phones_ok=(True,)):
        self.qr = ScriptedQR(qr_outcomes)
        self.passwords_ok = list(passwords_ok)
        self.codes_ok = list(codes_ok)
        self.phones_ok = list(phones_ok)
        self.session = object()
        self.disconnected = False
        self.passwords = []
        self.need_password_after_code = False
        self.logged_out = False
        self.me = SimpleNamespace(
            first_name="Анна", last_name="Петрова", username="anna_p", phone="79990001122"
        )

    async def get_me(self):
        return self.me

    async def log_out(self):
        self.logged_out = True

    async def connect(self):
        return None

    async def disconnect(self):
        self.disconnected = True

    async def qr_login(self):
        return self.qr

    async def send_code_request(self, phone):
        if not self.phones_ok.pop(0):
            raise errors.PhoneNumberInvalidError(request=None)

    async def sign_in(self, phone=None, code=None, password=None):
        if password is not None:
            self.passwords.append(password)
            if not self.passwords_ok.pop(0):
                raise errors.PasswordHashInvalidError(request=None)
            return "user"
        if not self.codes_ok.pop(0):
            raise errors.PhoneCodeInvalidError(request=None)
        if self.need_password_after_code:
            raise errors.SessionPasswordNeededError(request=None)
        return "user"


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """Point the command at a temp config.env holding api_id/api_hash."""
    path = tmp_path / "config.env"
    path.write_text("TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=abcdef\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    monkeypatch.setattr(terminal_login.StringSession, "save", staticmethod(lambda session: "SESSION-STRING"))

    state = {}

    def use(client, inputs=(), passwords=(), confirm=""):
        inputs, passwords = list(inputs), list(passwords)
        state["client"] = client
        monkeypatch.setattr(terminal_login, "TelegramClient", lambda *args, **kwargs: client)

        def fake_input(prompt=""):
            # The "is this the right account" question is answered separately, so
            # that scripted phone/code inputs keep their order.
            if prompt.startswith("Это нужный аккаунт"):
                return confirm
            return inputs.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        monkeypatch.setattr(terminal_login.getpass, "getpass", lambda prompt="": passwords.pop(0))
        return client

    return path, use


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_qr_login_writes_session_and_asks_nothing(setup, capsys):
    path, use = setup
    client = use(FakeClient(qr_outcomes=["user"]))

    terminal_login.main([])

    content = path.read_text(encoding="utf-8")
    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in content
    assert "TELEGRAM_API_ID=12345" in content
    assert "SESSION-STRING" not in capsys.readouterr().out
    assert client.disconnected
    if os.name == "posix":
        assert (path.stat().st_mode & 0o777) == 0o600


def test_expired_qr_is_replaced(setup):
    path, use = setup
    client = use(FakeClient(qr_outcomes=[asyncio.TimeoutError, asyncio.TimeoutError, "user"]))

    terminal_login.main([])

    assert client.qr.recreated == 2
    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in path.read_text(encoding="utf-8")


def test_unscanned_qr_falls_back_to_phone(setup):
    path, use = setup
    outcomes = [asyncio.TimeoutError] * terminal_login.QR_MAX_REFRESHES
    use(FakeClient(qr_outcomes=outcomes), inputs=["+79990000000", "12345"])

    terminal_login.main([])

    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in path.read_text(encoding="utf-8")


def test_qr_with_cloud_password_retries_wrong_password(setup):
    path, use = setup
    client = use(
        FakeClient(qr_outcomes=[errors.SessionPasswordNeededError], passwords_ok=[False, True]),
        passwords=["wrong", "right"],
    )

    terminal_login.main([])

    assert client.passwords == ["wrong", "right"]
    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in path.read_text(encoding="utf-8")


def test_phone_flag_skips_qr_and_retries_bad_number_and_code(setup):
    path, use = setup
    client = FakeClient(phones_ok=[False, True], codes_ok=[False, True])
    client.need_password_after_code = True
    use(client, inputs=["123", "+79990000000", "00000", "12345"], passwords=["pw"])

    terminal_login.main(["--phone"])

    assert client.qr.outcomes == []
    assert client.passwords == ["pw"]
    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in path.read_text(encoding="utf-8")


def test_bad_api_credentials_fail_without_phone_fallback(setup, capsys):
    path, use = setup
    use(FakeClient(qr_outcomes=[errors.ApiIdInvalidError]))

    with pytest.raises(SystemExit) as exc:
        terminal_login.main([])

    assert exc.value.code == terminal_login.EXIT_BAD_CREDENTIALS
    assert "api_id" in capsys.readouterr().err
    assert "TELEGRAM_SESSION_STRING" not in path.read_text(encoding="utf-8")


def test_single_named_account_is_relogged_in_under_its_own_key(setup):
    path, use = setup
    path.write_text(
        "TELEGRAM_API_ID=12345\n"
        "TELEGRAM_API_HASH=abcdef\n"
        "# keep me\n"
        "TELEGRAM_SESSION_STRING_WORK=dead-session\n"
        "TELEGRAM_EXPOSED_TOOLS=read-only\n",
        encoding="utf-8",
    )
    use(FakeClient(qr_outcomes=["user"]))

    terminal_login.main([])

    content = path.read_text(encoding="utf-8")
    assert "dead-session" not in content
    assert "TELEGRAM_SESSION_STRING_WORK=SESSION-STRING" in content
    assert "TELEGRAM_SESSION_STRING=" not in content.replace("TELEGRAM_SESSION_STRING_WORK=", "")
    assert "# keep me" in content
    assert "TELEGRAM_EXPOSED_TOOLS=read-only" in content


def _two_accounts(path):
    path.write_text(
        "TELEGRAM_API_ID=12345\n"
        "TELEGRAM_API_HASH=abcdef\n"
        "TELEGRAM_SESSION_STRING=personal-session\n"
        "TELEGRAM_SESSION_STRING_WORK=old-work-session\n"
        "TELEGRAM_DEFAULT_ACCOUNT=work\n"
        # load() adds this line if it is missing; present, the file stays as is.
        "TELEGRAM_EXPOSED_TOOLS=read-only\n",
        encoding="utf-8",
    )


def test_account_flag_replaces_only_that_accounts_key(setup):
    path, use = setup
    _two_accounts(path)
    use(FakeClient(qr_outcomes=["user"]))

    terminal_login.main(["--account", "Work"])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "TELEGRAM_SESSION_STRING=personal-session" in lines
    assert "TELEGRAM_SESSION_STRING_WORK=SESSION-STRING" in lines
    assert not any("old-work-session" in line for line in lines)
    assert "TELEGRAM_DEFAULT_ACCOUNT=work" in lines


def test_account_flag_adds_a_new_account_beside_the_first(setup):
    path, use = setup
    path.write_text(
        "TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=abcdef\nTELEGRAM_SESSION_STRING=personal-session\n",
        encoding="utf-8",
    )
    use(FakeClient(qr_outcomes=["user"]))

    terminal_login.main(["--account", "work"])

    lines = path.read_text(encoding="utf-8").splitlines()
    assert "TELEGRAM_SESSION_STRING=personal-session" in lines
    assert "TELEGRAM_SESSION_STRING_WORK=SESSION-STRING" in lines


def test_several_accounts_without_account_flag_write_nothing_and_name_the_labels(setup, capsys):
    path, use = setup
    _two_accounts(path)
    before = path.read_text(encoding="utf-8")
    client = use(FakeClient(qr_outcomes=["user"]))

    with pytest.raises(SystemExit) as exc:
        terminal_login.main([])

    assert exc.value.code == terminal_login.EXIT_AMBIGUOUS_ACCOUNT
    assert exc.value.code not in (0, 1, 2, terminal_login.EXIT_BAD_CREDENTIALS)
    err = capsys.readouterr().err
    assert "default" in err and "work" in err and "--account" in err
    assert path.read_text(encoding="utf-8") == before
    assert client.qr.outcomes == ["user"]  # never got as far as logging in


def test_login_shows_name_and_username_but_not_the_phone(setup, capsys):
    path, use = setup
    use(FakeClient(qr_outcomes=["user"]))

    terminal_login.main([])

    out = capsys.readouterr().out
    assert "Анна Петрова" in out and "@anna_p" in out
    assert "79990001122" not in out


def test_declining_the_account_writes_nothing_and_logs_the_new_session_out(setup):
    path, use = setup
    _two_accounts(path)
    before = path.read_text(encoding="utf-8")
    client = use(FakeClient(qr_outcomes=["user"]), confirm="н")

    with pytest.raises(SystemExit) as exc:
        terminal_login.main(["--account", "work"])

    assert exc.value.code == terminal_login.EXIT_DECLINED
    assert path.read_text(encoding="utf-8") == before
    assert client.logged_out and client.disconnected


def test_failing_get_me_still_asks_for_confirmation(setup):
    path, use = setup
    client = FakeClient(qr_outcomes=["user"])

    async def broken():
        raise RuntimeError("boom")

    client.get_me = broken
    use(client)

    terminal_login.main([])

    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("flags", [["--account", "рабочий"], ["--account", "a" * 33], ["--account", ""], ["--rename-default", "default"]])
def test_invalid_labels_are_rejected_by_the_parser(setup, flags, capsys):
    with pytest.raises(SystemExit) as exc:
        terminal_login.main(flags)

    assert exc.value.code == 2
    assert "Метка" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags",
    [
        ["--account", "work", "--list-accounts"],
        ["--rename-default", "a", "--set-default", "b"],
        ["--phone", "--list-accounts"],
        ["--phone", "--rename-default", "a"],
        ["--phone", "--set-default", "none"],
    ],
)
def test_modes_are_mutually_exclusive(setup, flags):
    with pytest.raises(SystemExit) as exc:
        terminal_login.main(flags)

    assert exc.value.code == 2


def test_phone_combines_with_account(setup):
    path, use = setup
    use(FakeClient(), inputs=["+79990000000", "12345"])

    terminal_login.main(["--phone", "--account", "work"])

    assert "TELEGRAM_SESSION_STRING_WORK=SESSION-STRING" in path.read_text(encoding="utf-8")


def test_list_accounts_prints_labels_without_values_or_network(setup, capsys, monkeypatch):
    path, use = setup
    _two_accounts(path)
    path.write_text(path.read_text(encoding="utf-8") + "TELEGRAM_SESSION_STRING_OLD=\n", encoding="utf-8")
    monkeypatch.setattr(terminal_login, "TelegramClient", lambda *a, **k: pytest.fail("network"))

    terminal_login.main(["--list-accounts"])

    out = capsys.readouterr().out
    assert out.splitlines() == ["default", "work (по умолчанию)"]
    assert "session" not in out.lower() and "abcdef" not in out


def test_list_accounts_without_any_prints_nothing(setup, capsys):
    terminal_login.main(["--list-accounts"])

    assert capsys.readouterr().out == ""


def test_rename_default_command(setup):
    path, use = setup
    _two_accounts(path)

    terminal_login.main(["--rename-default", "Personal"])

    keys = [line.split("=")[0] for line in path.read_text(encoding="utf-8").splitlines()]
    assert "TELEGRAM_SESSION_STRING_PERSONAL" in keys
    assert "TELEGRAM_SESSION_STRING" not in keys
    assert "TELEGRAM_SESSION_STRING_WORK" in keys


def test_rename_default_refuses_when_there_is_nothing_to_rename(setup, capsys):
    path, use = setup

    with pytest.raises(SystemExit) as exc:
        terminal_login.main(["--rename-default", "personal"])

    assert exc.value.code == 1
    assert "нет сохранённой сессии" in capsys.readouterr().err


def test_set_default_command_sets_and_clears(setup):
    path, use = setup
    _two_accounts(path)

    terminal_login.main(["--set-default", "default"])
    assert "TELEGRAM_DEFAULT_ACCOUNT=default" in path.read_text(encoding="utf-8")

    terminal_login.main(["--set-default", "none"])
    assert "TELEGRAM_DEFAULT_ACCOUNT" not in path.read_text(encoding="utf-8")


def test_set_default_to_an_unknown_account_is_refused(setup, capsys):
    path, use = setup
    _two_accounts(path)

    with pytest.raises(SystemExit) as exc:
        terminal_login.main(["--set-default", "nobody"])

    assert exc.value.code == 1
    assert "work" in capsys.readouterr().err
    assert "TELEGRAM_DEFAULT_ACCOUNT=work" in path.read_text(encoding="utf-8")


def test_missing_credentials_exit_with_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.env")

    with pytest.raises(SystemExit) as exc:
        terminal_login.main([])

    assert exc.value.code == 1
    assert "api_id" in capsys.readouterr().err
