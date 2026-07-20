import asyncio
import os
import stat
import sys

import pytest

from telegram_mcp_ag import config, login

TELEGRAM_ENV_PREFIX = "TELEGRAM_"


@pytest.fixture(autouse=True)
def clean_telegram_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(TELEGRAM_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def reset_login_state():
    login._state.update({"qr": None, "task": None, "qr_path": None, "tmpdir": None})
    yield
    login._state.update({"qr": None, "task": None, "qr_path": None, "tmpdir": None})


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeToolAnnotations:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeMCP:
    """Captures tools the way FastMCP's decorator registers them."""

    def __init__(self):
        self.tools = {}

    def tool(self, annotations=None):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakeQRLogin:
    def __init__(self, url="tg://login?token=Zm9vYmFy"):
        self.url = url
        self.recreated = 0
        self._future = None

    @property
    def result(self):
        """The future the scan resolves. Created lazily: the fixture that builds
        this object runs outside the event loop."""
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        return self._future

    async def recreate(self):
        self.recreated += 1
        self.url = f"{self.url}-new"
        self._future = None

    async def wait(self, timeout=None):
        return await self.result


class FakeUser:
    first_name = "Тест"
    username = "test"


class FakeSession:
    pass


class FakeClient:
    def __init__(self, authorized=False):
        self.authorized = authorized
        self.session = FakeSession()
        self.qr = FakeQRLogin()
        self.signed_in_with = None

    def is_connected(self):
        return True

    async def connect(self):
        return None

    async def is_user_authorized(self):
        return self.authorized

    async def qr_login(self):
        return self.qr

    async def get_me(self):
        return FakeUser()

    async def sign_in(self, password=None):
        self.signed_in_with = password
        self.authorized = True
        return FakeUser()


@pytest.fixture
def registered(monkeypatch, tmp_path):
    """Register the login tools against a fake client and a temp config.env."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.env")
    monkeypatch.setattr(login.StringSession, "save", staticmethod(lambda session: "SESSION-STRING"))
    monkeypatch.setattr(login, "_open_in_viewer", lambda path: False)

    client = FakeClient()
    mcp = FakeMCP()
    names = login.register(mcp, FakeToolAnnotations, lambda account: client)
    assert names == ["telegram_login_start", "telegram_login_check"]
    return mcp.tools, client, tmp_path / "config.env"


# ---------------------------------------------------------------------------
# QR rendering
# ---------------------------------------------------------------------------


def test_render_qr_png_produces_a_png():
    png = render = login.render_qr_png("tg://login?token=abc")
    assert render[:8] == b"\x89PNG\r\n\x1a\n"
    # Well under the ~1 MB ceiling MCP hosts put on tool content.
    assert len(png) < 100_000


def test_qr_file_is_owner_only(monkeypatch):
    monkeypatch.setattr(login, "_open_in_viewer", lambda path: False)
    path, opened = login._write_qr_file(login.render_qr_png("tg://login?token=abc"))
    try:
        assert path is not None and not opened
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        login._cleanup_qr_file()
    assert not path.exists()


# ---------------------------------------------------------------------------
# The login conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_returns_instructions_and_an_image(registered):
    tools, _client, _config_path = registered

    text, image = await tools["telegram_login_start"]()

    assert "Устройства" in text
    assert "tg://login?token=" in text
    assert image.to_image_content().mimeType == "image/png"


@pytest.mark.asyncio
async def test_check_reports_still_waiting_without_blocking_forever(registered, monkeypatch):
    tools, _client, _config_path = registered
    monkeypatch.setattr(login, "WAIT_TIMEOUT_SECONDS", 0.05)

    await tools["telegram_login_start"]()
    (message,) = await tools["telegram_login_check"]()

    assert "Пока жду сканирования" in message


@pytest.mark.asyncio
async def test_check_before_start_says_so(registered):
    tools, _client, _config_path = registered

    (message,) = await tools["telegram_login_check"]()

    assert "telegram_login_start" in message


@pytest.mark.asyncio
async def test_expired_token_is_replaced_with_a_fresh_code(registered, monkeypatch):
    tools, client, _config_path = registered
    monkeypatch.setattr(login, "WAIT_TIMEOUT_SECONDS", 5)

    await tools["telegram_login_start"]()
    # Telethon signals an expired login token by failing the wait with TimeoutError.
    client.qr.result.set_exception(asyncio.TimeoutError())

    text, image = await tools["telegram_login_check"]()

    assert client.qr.recreated == 1
    assert "устарел" in text
    assert image.to_image_content().mimeType == "image/png"


@pytest.mark.asyncio
async def test_successful_scan_writes_the_session_and_never_prints_it(registered, monkeypatch):
    tools, client, config_path = registered
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")

    await tools["telegram_login_start"]()
    client.qr.result.set_result(FakeUser())
    (message,) = await tools["telegram_login_check"]()

    assert "Вход выполнен" in message
    assert "SESSION-STRING" not in message

    written = config_path.read_text(encoding="utf-8")
    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in written
    assert "TELEGRAM_API_ID=123" in written
    if os.name == "posix":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_already_authorized_account_is_persisted_not_re_scanned(registered):
    tools, client, config_path = registered
    client.authorized = True

    (message,) = await tools["telegram_login_check"]()

    assert "Вход выполнен" in message
    assert "TELEGRAM_SESSION_STRING=SESSION-STRING" in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_restart_is_requested_when_tools_cannot_be_activated(registered):
    tools, client, _config_path = registered
    client.authorized = True

    (message,) = await tools["telegram_login_check"]()

    # No activation hook is registered in this fixture, which is the same
    # situation as a host that refuses the tools/list_changed notification.
    assert "перезапустите" in message.lower() or "включите" in message


# ---------------------------------------------------------------------------
# Two-factor authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_2fa_password_is_asked_locally_and_not_echoed(registered, monkeypatch):
    tools, client, config_path = registered
    asked = {}

    async def fake_ask():
        asked["called"] = True
        return "cloud-password"

    monkeypatch.setattr(login, "_ask_password", fake_ask)

    await tools["telegram_login_start"]()
    client.qr.result.set_exception(login.SessionPasswordNeededError(request=None))
    (message,) = await tools["telegram_login_check"]()

    assert asked["called"]
    assert client.signed_in_with == "cloud-password"
    assert "Вход выполнен" in message
    # The password must never come back out through the conversation.
    assert "cloud-password" not in message
    assert "cloud-password" not in config_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_2fa_falls_back_to_the_installer_without_a_dialog(registered, monkeypatch):
    tools, client, _config_path = registered

    async def no_dialog():
        return None

    monkeypatch.setattr(login, "_ask_password", no_dialog)

    await tools["telegram_login_start"]()
    client.qr.result.set_exception(login.SessionPasswordNeededError(request=None))
    (message,) = await tools["telegram_login_check"]()

    assert "install.sh" in message
    assert "переписку" in message


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX dialog lookup")
def test_no_password_prompt_command_on_a_headless_box(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(login.shutil, "which", lambda name: None)

    assert login._password_prompt_command() is None
