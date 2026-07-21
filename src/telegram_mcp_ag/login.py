"""QR login from the chat, for hosts that have no terminal.

Claude Desktop installs the .mcpb bundle through a one-shot form: it can
collect ``api_id``/``api_hash``, but it has no way to run an interactive
Telegram login, and the upstream server refuses to start without a session.
That forced every Claude Desktop user through ``install.sh`` — the exact
terminal step the bundle exists to avoid.

So when credentials are present but a session is not, ``server.py`` starts in
login-only mode and exposes just the tools below. Only a one-shot login token
travels through the conversation: no phone number, no code, no cloud password,
no session string. The token is bound to this client's key and is confirmed on
the user's phone, so intercepting the picture is not enough to sign in.
"""

import asyncio
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import qrcode
from mcp.server.fastmcp import Image
from qrcode.image.pure import PyPNGImage
from telethon.errors import ApiIdInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

from telegram_mcp_ag import config as _config

# How long a single tool call may block waiting for the scan. MCP hosts time
# calls out on their own, so we return "still waiting" well before that and let
# the model call again rather than risk the host killing the request.
WAIT_TIMEOUT_SECONDS = 25.0
PASSWORD_PROMPT_TIMEOUT_SECONDS = 180.0

_PROMPT_TITLE = "Telegram: облачный пароль"
_PROMPT_TEXT = "Введите облачный пароль (2FA) вашего аккаунта Telegram"

_BAD_CREDENTIALS_MESSAGE = (
    "Telegram не принял api_id и api_hash. Проверьте их в настройках расширения: "
    "они берутся на my.telegram.org/apps → API development tools, где api_id — "
    "число, а api_hash — длинная строка рядом с ним. Чаще всего их путают местами "
    "или копируют с лишним пробелом."
)

_state: dict = {
    "qr": None,
    "task": None,
    "qr_path": None,
    "tmpdir": None,
    "activate": None,
}


def set_activation_hook(callback) -> None:
    """Register what to run once the account is authorized (see ``server.py``)."""
    _state["activate"] = callback


# ---------------------------------------------------------------------------
# Showing the QR code
# ---------------------------------------------------------------------------


def render_qr_png(url: str) -> bytes:
    """Render ``url`` as a PNG QR code.

    Uses qrcode's pure-Python PNG writer instead of the Pillow one: the bundle
    is installed by uv on the user's machine, and Pillow is a large wheel to
    pull in for one black-and-white image.
    """
    buffer = io.BytesIO()
    qrcode.make(url, image_factory=PyPNGImage, box_size=8, border=4).save(buffer)
    return buffer.getvalue()


def _open_in_viewer(path: Path) -> bool:
    """Open ``path`` with the OS image viewer. Returns whether it was launched.

    MCP hosts collapse tool-result images behind an expander or drop them
    altogether, so the picture in the chat cannot be the only channel. The
    server runs on the user's own machine, so it can just show the file.
    """
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif os.name == "nt":
            os.startfile(str(path))  # noqa: S606 - Windows' own file association
        else:
            if not shutil.which("xdg-open"):
                return False
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True
    except Exception:
        return False


def _write_qr_file(png: bytes) -> tuple[Optional[Path], bool]:
    """Write the QR to a private temp file and try to open it."""
    try:
        tmpdir = _state["tmpdir"]
        if tmpdir is None:
            tmpdir = Path(tempfile.mkdtemp(prefix="telegram-mcp-ag-login-"))
            _state["tmpdir"] = tmpdir
        path = tmpdir / "telegram-login-qr.png"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(png)
        _state["qr_path"] = path
        return path, _open_in_viewer(path)
    except Exception:
        return None, False


def _cleanup_qr_file() -> None:
    tmpdir = _state.pop("tmpdir", None)
    _state["qr_path"] = None
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The cloud password (2FA)
# ---------------------------------------------------------------------------


def _password_prompt_command() -> Optional[list]:
    """Build a command that asks for a password in a native OS dialog.

    The password is the account's master credential, so it must not travel
    through the conversation, and it is needed exactly once — storing it in the
    bundle's config just to replay it later would be worse than the session
    string it unlocks. A local dialog keeps it off the wire and out of any file.
    """
    if sys.platform == "darwin":
        script = (
            f'display dialog "{_PROMPT_TEXT}" with title "{_PROMPT_TITLE}" '
            'default answer "" with hidden answer'
        )
        return ["osascript", "-e", script, "-e", "text returned of result"]

    if os.name == "nt":
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic;"
            f"$s = Read-Host -AsSecureString -Prompt '{_PROMPT_TEXT}';"
            "[Runtime.InteropServices.Marshal]::PtrToStringAuto("
            "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"
        )
        for shell in ("pwsh", "powershell"):
            if shutil.which(shell):
                return [shell, "-NoProfile", "-Command", script]
        return None

    if shutil.which("zenity"):
        return ["zenity", "--password", f"--title={_PROMPT_TITLE}"]
    if shutil.which("kdialog"):
        return ["kdialog", "--title", _PROMPT_TITLE, "--password", _PROMPT_TEXT]
    return None


async def _ask_password() -> Optional[str]:
    """Ask for the cloud password locally. ``None`` if no dialog is available."""
    command = _password_prompt_command()
    if command is None:
        return None

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=PASSWORD_PROMPT_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        process.kill()
        return None

    if process.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


# ---------------------------------------------------------------------------
# Finishing the login
# ---------------------------------------------------------------------------


async def _persist_session(client) -> str:
    """Save the freshly authorized session and switch the server to full mode."""
    # StringSession.save() is an unbound method that works on any Session, which
    # is how Telethon's own session_string_generator exports a file session.
    session_string = StringSession.save(client.session)

    values = {"TELEGRAM_SESSION_STRING": session_string}
    # The bundle passes credentials through the form, so config.env may not
    # exist yet. Writing all three keeps every client working off one file,
    # which is where this project keeps secrets.
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        value = os.environ.get(key, "").strip()
        if value:
            values[key] = value
    _config.write_values(values)

    os.environ["TELEGRAM_SESSION_STRING"] = session_string
    _cleanup_qr_file()

    activate = _state.get("activate")
    if activate is None:
        return "restart"
    try:
        return await activate()
    except Exception as exc:
        print(f"telegram-mcp-ag: could not activate tools in place: {exc}", file=sys.stderr)
        return "restart"


def _success_message(user, activation: str) -> str:
    name = getattr(user, "first_name", None) or getattr(user, "username", None) or "аккаунт"
    lines = [
        f"Вход выполнен: {name}.",
        f"Сессия сохранена в {_config.CONFIG_PATH} (доступ только владельцу файла).",
    ]
    if activation == "activated":
        lines.append(
            "Инструменты чтения Telegram и расшифровки голосовых уже доступны — "
            "можно сразу просить сводку по чатам."
        )
    else:
        lines.append(
            "Чтобы инструменты чтения появились, выключите и снова включите "
            "расширение Telegram в настройках (или перезапустите приложение). "
            "Это нужно один раз."
        )
    lines.append(
        "Если при первом вызове инструмента появится запрос подтверждения — "
        "нажмите «Always Allow», чтобы больше не спрашивало."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def register(mcp, ToolAnnotations, get_client) -> list:
    """Register the login tools and return their names."""

    async def _refresh_qr() -> list:
        """Issue a new login token after the previous one expired."""
        qr = _state["qr"]
        await qr.recreate()
        _state["task"] = asyncio.create_task(qr.wait(timeout=None))

        png = render_qr_png(qr.url)
        _, opened = _write_qr_file(png)

        note = "Прежний QR-код устарел, вот новый — отсканируйте его."
        if opened:
            note += " Он открыт в окне на этом компьютере."
        note += (
            "\nПосле сканирования вызовите telegram_login_check ещё раз."
            f"\nЗапасная ссылка: {qr.url}"
        )
        return [note, Image(data=png, format="png")]

    async def _finish_with_password(client) -> list:
        """Complete a login on an account protected by a cloud password."""
        password = await _ask_password()
        if password is None:
            return [
                "У этого аккаунта включён облачный пароль (2FA), а запросить его "
                "системным окном на этом компьютере не удалось. Пароль нельзя "
                "вводить в переписку, поэтому завершите вход установщиком в "
                "терминале — он спросит пароль локально:\n"
                "curl -fsSL https://raw.githubusercontent.com/agroznykh/"
                "telegram-mcp-ag/main/install.sh | bash"
            ]
        try:
            user = await client.sign_in(password=password)
        except Exception as exc:
            return [
                f"Облачный пароль не подошёл ({exc}). Вызовите telegram_login_start "
                "и попробуйте ещё раз."
            ]
        finally:
            del password

        activation = await _persist_session(client)
        return [_success_message(user, activation)]

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Log in to Telegram (QR)",
            openWorldHint=True,
            readOnlyHint=True,
        )
    )
    async def telegram_login_start() -> list:
        """Start the Telegram login and show a QR code to scan.

        Call this first when no Telegram account is connected yet. Then tell the
        user to open Telegram on their phone and go to Settings → Devices →
        Link Desktop Device, and scan the code. After that call
        ``telegram_login_check`` to finish the login; it may need calling more
        than once while the user is scanning.
        """
        try:
            client = get_client(None)
            if not client.is_connected():
                await client.connect()
            if await client.is_user_authorized():
                return ["Этот аккаунт Telegram уже подключён, вход не нужен."]

            previous = _state.get("task")
            if previous is not None and not previous.done():
                previous.cancel()

            qr = await client.qr_login()
            _state["qr"] = qr
            _state["task"] = asyncio.create_task(qr.wait(timeout=None))

            png = render_qr_png(qr.url)
            path, opened = _write_qr_file(png)

            instructions = [
                "Чтобы подключить Telegram, отсканируйте QR-код телефоном:",
                "Telegram → Настройки → Устройства → Подключить устройство.",
            ]
            if opened:
                instructions.append("Код открыт в отдельном окне на этом компьютере.")
            elif path is not None:
                instructions.append(f"Код также сохранён в файл: {path}")
            instructions.append(
                "Если код не видно, откройте эту ссылку на компьютере, где уже "
                f"установлен Telegram: {qr.url}"
            )
            instructions.append(
                "После сканирования вызовите telegram_login_check, чтобы завершить вход."
            )

            return ["\n".join(instructions), Image(data=png, format="png")]
        except ApiIdInvalidError:
            return [_BAD_CREDENTIALS_MESSAGE]
        except Exception as exc:
            return [f"Не удалось начать вход в Telegram: {exc}"]

    @mcp.tool(
        annotations=ToolAnnotations(
            title="Finish Telegram Login",
            openWorldHint=True,
            readOnlyHint=True,
        )
    )
    async def telegram_login_check() -> list:
        """Check whether the QR code has been scanned and finish the login.

        Call after ``telegram_login_start``. If the user has not scanned yet
        this returns a "still waiting" note — say so and call again. If the code
        expired it returns a fresh one to show.
        """
        try:
            client = get_client(None)
            if await client.is_user_authorized():
                activation = await _persist_session(client)
                return [_success_message(await client.get_me(), activation)]

            task = _state.get("task")
            qr = _state.get("qr")
            if task is None or qr is None:
                return ["Вход ещё не начат — сначала вызовите telegram_login_start."]

            try:
                user = await asyncio.wait_for(
                    asyncio.shield(task), timeout=WAIT_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                # Telethon raises TimeoutError too, when the login token itself
                # expires. Only the task finishing tells the two apart: ours
                # means the user is still scanning, Telethon's means the code on
                # screen is dead and has to be replaced.
                if task.done():
                    return await _refresh_qr()
                return [
                    "Пока жду сканирования. Попросите пользователя отсканировать "
                    "код (Telegram → Настройки → Устройства → Подключить "
                    "устройство) и вызовите telegram_login_check ещё раз."
                ]
            except SessionPasswordNeededError:
                return await _finish_with_password(client)

            activation = await _persist_session(client)
            return [_success_message(user, activation)]
        except Exception as exc:
            return [f"Не удалось завершить вход в Telegram: {exc}"]

    return ["telegram_login_start", "telegram_login_check"]
