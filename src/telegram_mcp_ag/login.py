"""QR login from the chat, for whenever a session goes stale without a terminal at hand.

The normal path (``install.sh``) already does an interactive terminal QR
login and leaves every registered client — Claude Code, Codex, Claude
Desktop — pointed at the resulting session. This module exists for what
happens after that: a session revoked from the phone, or ``AUTH_KEY_DUPLICATED``
from reusing it on a second machine. The user may not have that terminal
open anymore, so re-authenticating has to be possible from inside the chat.

When credentials are present but a session is not, ``server.py`` starts in
login-only mode and exposes just the tools below. Only a one-shot login token
travels through the conversation: no phone number, no code, no cloud password,
no session string. The token is bound to this client's key and is confirmed on
the user's phone, so intercepting the picture is not enough to sign in.
"""

import asyncio
import base64
import html
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import qrcode
from qrcode.image.pure import PyPNGImage
from telethon.errors import ApiIdInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

from telegram_mcp_ag import config as _config
from telegram_mcp_ag import relogin as _relogin

# How long a single tool call may block waiting for the scan. MCP hosts time
# calls out on their own, so we return "still waiting" well before that and let
# the model call again rather than risk the host killing the request.
WAIT_TIMEOUT_SECONDS = 25.0
PASSWORD_PROMPT_TIMEOUT_SECONDS = 180.0

_PROMPT_TITLE = "Telegram: облачный пароль"
_PROMPT_TEXT = "Введите облачный пароль (2FA) вашего аккаунта Telegram"

def _bad_credentials_message() -> str:
    return (
        "Telegram не принял api_id и api_hash, сохранённые при установке. Чтобы "
        "ввести их заново, запустите установщик ещё раз -- он сам их спросит. "
        "Ключи берутся на my.telegram.org/apps -> API development tools, где "
        "api_id -- число, а api_hash -- длинная строка рядом с ним. Чаще всего их "
        "путают местами или копируют с лишним пробелом. "
        # Last, with nothing after it: a trailing period copied along with the
        # command would turn --relogin into an unknown flag.
        f"{_relogin.instructions()}"
    )

_state: dict = {
    "qr": None,
    "task": None,
    "page_path": None,
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

    Uses qrcode's pure-Python PNG writer instead of the Pillow one: Pillow is
    a large wheel to depend on for one black-and-white image.
    """
    buffer = io.BytesIO()
    qrcode.make(url, image_factory=PyPNGImage, box_size=8, border=4).save(buffer)
    return buffer.getvalue()


def _open_in_viewer(path: Path) -> bool:
    """Open ``path`` with its default OS app. Returns whether it was launched.

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


# A login token lives about half a minute, so a picture shown once goes stale
# long before a user gets their phone out. The code is therefore shown on a
# local page that reloads itself, and the server swaps a fresh code into it
# whenever the old one expires (see _wait_with_fresh_codes).
PAGE_RELOAD_SECONDS = 3
# Fresh codes issued without the assistant's help: about five minutes' worth.
QR_MAX_CODES = 10
# How long the "done" page outlives the login, for the browser's next reload.
_DONE_PAGE_SECONDS = 60.0

_PAGE_STYLE = (
    "body{font-family:-apple-system,system-ui,sans-serif;text-align:center;"
    "margin:40px;color:#222}img{width:320px;height:320px;image-rendering:pixelated}"
)


def _qr_page(png: bytes, url: str) -> str:
    # The image is inlined: a separate file would be cached by the browser and
    # the reload would keep showing the old code.
    image = base64.b64encode(png).decode("ascii")
    link = html.escape(url, quote=True)
    return (
        '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="{PAGE_RELOAD_SECONDS}">'
        f"<title>Вход в Telegram</title><style>{_PAGE_STYLE}</style></head><body>"
        "<h1>Вход в Telegram</h1>"
        "<p>Отсканируйте код телефоном: Telegram → Настройки → Устройства → "
        "Подключить устройство.</p>"
        f'<img src="data:image/png;base64,{image}" alt="QR-код входа в Telegram">'
        "<p>Код меняется каждые полминуты — эта страница сама показывает актуальный.</p>"
        f'<p>Если Telegram установлен на этом компьютере: <a href="{link}">войти по ссылке</a>.</p>'
        "</body></html>"
    )


_DONE_PAGE = (
    '<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
    f"<title>Вход в Telegram</title><style>{_PAGE_STYLE}</style></head><body>"
    "<h1>Вход выполнен</h1><p>Это окно можно закрыть.</p></body></html>"
)


def _write_page(content: str) -> Optional[Path]:
    """Write the login page to a private temp dir. ``None`` if that failed."""
    try:
        tmpdir = _state["tmpdir"]
        if tmpdir is None:
            tmpdir = Path(tempfile.mkdtemp(prefix="telegram-mcp-ag-login-"))
            _state["tmpdir"] = tmpdir
        path = tmpdir / "telegram-login.html"
        # Written aside and renamed into place: the browser reloads on its own
        # schedule and must never catch a half-written page.
        partial = tmpdir / "telegram-login.html.partial"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(partial, path)
        _state["page_path"] = path
        return path
    except Exception:
        return None


def _show_qr_page(png: bytes, url: str, open_it: bool) -> tuple[Optional[Path], bool]:
    """Put the current code on the page; open the page in the browser if asked.

    Returns the page path (``None`` if it couldn't be written) and whether it
    was opened. A refresh needs no opening: the page already on screen reloads.
    """
    path = _write_page(_qr_page(png, url))
    if path is None or not open_it:
        return path, False
    return path, _open_in_viewer(path)


def _finish_qr_page() -> None:
    """Replace the code with a "done" page, then remove it a little later."""
    tmpdir = _state["tmpdir"]
    if tmpdir is None:
        return
    _write_page(_DONE_PAGE)
    _state["tmpdir"] = None
    _state["page_path"] = None
    try:
        asyncio.get_running_loop().call_later(
            _DONE_PAGE_SECONDS, shutil.rmtree, tmpdir, True
        )
    except RuntimeError:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _wait_with_fresh_codes(qr):
    """Wait for the scan, replacing each expired code with a fresh one.

    Raises ``asyncio.TimeoutError`` once ``QR_MAX_CODES`` codes have expired
    unscanned; the next ``telegram_login_check`` then starts over.
    """
    for attempt in range(QR_MAX_CODES):
        try:
            # Without a timeout Telethon waits exactly until this code expires.
            return await qr.wait()
        except asyncio.TimeoutError:
            if attempt == QR_MAX_CODES - 1:
                raise
            await qr.recreate()
            _show_qr_page(render_qr_png(qr.url), qr.url, open_it=False)


# ---------------------------------------------------------------------------
# The cloud password (2FA)
# ---------------------------------------------------------------------------


def _password_prompt_command() -> Optional[list]:
    """Build a command that asks for a password in a native OS dialog.

    The password is the account's master credential, so it must not travel
    through the conversation, and it is needed exactly once — storing it in
    config.env just to replay it later would be worse than the session
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
    # The credentials may have come from the environment rather than
    # config.env. Writing all three keeps every client working off one file,
    # which is where this project keeps secrets.
    for key in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        value = os.environ.get(key, "").strip()
        if value:
            values[key] = value
    _config.write_values(values)

    os.environ["TELEGRAM_SESSION_STRING"] = session_string
    _finish_qr_page()

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
    # activation == "activated" only means the tools/list_changed notification
    # was sent without the write itself raising -- it says nothing about
    # whether this particular MCP client actually re-fetches the tool list
    # inside an already-open conversation. Observed in practice: Claude
    # Desktop often doesn't, and the assistant then improvises (tries a
    # browser, claims the tools are missing) instead of relaying the one fix
    # that reliably works. So the restart instruction always ships, worded as
    # a fallback rather than a certainty either way.
    if activation == "activated":
        lines.append(
            "Инструменты чтения Telegram и расшифровки голосовых должны быть "
            "уже доступны в этом разговоре — можно сразу просить сводку по "
            "чатам. Если инструменты всё же не находятся (ассистент не видит "
            "их в списке доступных), это ограничение конкретного MCP-клиента, "
            f"а не проблема входа: {_relogin.RESTART_CLIENT} -- это нужно сделать один раз."
        )
    else:
        lines.append(
            f"Чтобы инструменты чтения появились, {_relogin.RESTART_CLIENT}. Это нужно один раз."
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
        """Start over once every code the server issued on its own expired."""
        qr = _state["qr"]
        await qr.recreate()
        _state["task"] = asyncio.create_task(_wait_with_fresh_codes(qr))

        png = render_qr_png(qr.url)
        # Opened again: after this long the user may have closed the window.
        path, opened = _show_qr_page(png, qr.url, open_it=True)

        note = "Прежние QR-коды устарели, сервер выдал новый."
        if opened:
            note += " Он открыт в окне браузера на этом компьютере и там сам обновляется."
        elif path is not None:
            note += f" Он на странице {path} — откройте её в браузере, она сама обновляется."
        note += (
            "\nПосле сканирования вызовите telegram_login_check ещё раз."
            f"\nЗапасная ссылка: {qr.url}"
        )
        return [note]

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

        Never ask the user to show or paste anything from config.env, not even
        key names: it holds the session, which is full access to the account.
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
            _state["task"] = asyncio.create_task(_wait_with_fresh_codes(qr))

            png = render_qr_png(qr.url)
            path, opened = _show_qr_page(png, qr.url, open_it=True)

            instructions = [
                "Чтобы подключить Telegram, отсканируйте QR-код телефоном:",
                "Telegram → Настройки → Устройства → Подключить устройство.",
            ]
            if opened:
                instructions.append(
                    "Код открыт в окне браузера на этом компьютере: код меняется "
                    "каждые полминуты, и окно само показывает актуальный."
                )
            elif path is not None:
                instructions.append(
                    f"Код на странице {path} — откройте её в браузере, она сама "
                    "показывает актуальный."
                )
            instructions.append(
                "Если код не видно, откройте эту ссылку на компьютере, где уже "
                f"установлен Telegram: {qr.url}"
            )
            instructions.append(
                "После сканирования вызовите telegram_login_check, чтобы завершить вход."
            )

            # No picture in the chat: a code there is dead within half a
            # minute, and a stale one is just a scan that fails.
            return ["\n".join(instructions)]
        except ApiIdInvalidError:
            return [_bad_credentials_message()]
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
                # The task raises TimeoutError too, once every code it issued
                # expired unscanned. Only the task finishing tells the two
                # apart: ours means the user is still scanning, the task's
                # means it gave up and the login has to start over.
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
