import asyncio
import os

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

    def use(client, inputs=(), passwords=()):
        inputs, passwords = list(inputs), list(passwords)
        state["client"] = client
        monkeypatch.setattr(terminal_login, "TelegramClient", lambda *args, **kwargs: client)
        monkeypatch.setattr("builtins.input", lambda prompt="": inputs.pop(0))
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

    assert exc.value.code == 1
    assert "api_id" in capsys.readouterr().err
    assert "TELEGRAM_SESSION_STRING" not in path.read_text(encoding="utf-8")


def test_missing_credentials_exit_with_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.env")

    with pytest.raises(SystemExit) as exc:
        terminal_login.main([])

    assert exc.value.code == 1
    assert "api_id" in capsys.readouterr().err
