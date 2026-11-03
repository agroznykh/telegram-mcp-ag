"""Terminal login for the installers: QR code first, phone number as the fallback.

Replaces upstream's ``telegram-mcp-generate-session``. That script asks
questions that make no sense for this project (an account label, whether to
write a ``.env`` file), prints the session string to the screen, and the
installers had to fish it back out of a transcript. This command asks only
what the login itself needs and merges the session straight into
``config.env``, which the installer has already written with
``api_id``/``api_hash``. The session string is never printed.
"""

import argparse
import asyncio
import getpass
import io
import os
import sys

import qrcode
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from telegram_mcp_ag import config as _config

# Each code lives about half a minute, so this is roughly five minutes of waiting.
QR_MAX_REFRESHES = 10
PHONE_ATTEMPTS = 3

_BAD_CREDENTIALS_MESSAGE = (
    "Telegram не принял api_id и api_hash. Проверьте их на my.telegram.org/apps "
    "-> API development tools: api_id -- число, api_hash -- длинная строка рядом "
    "с ним. Чаще всего их путают местами или копируют с лишним пробелом."
)


class LoginFailed(Exception):
    """A login that cannot go on. The message is shown to the user as is."""


# ---------------------------------------------------------------------------
# QR code
# ---------------------------------------------------------------------------


def _can_encode(text: str, stream) -> bool:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def render_qr_text(url: str) -> str:
    """Draw ``url`` as a QR code out of block characters."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    buffer = io.StringIO()
    qr.print_ascii(out=buffer, invert=True)
    return buffer.getvalue()


def _show_qr(url: str) -> None:
    art = render_qr_text(url)
    print()
    # A legacy Windows console in a single-byte code page cannot print the
    # block characters; the link still works there.
    if _can_encode(art, sys.stdout):
        print(art)
        print("Отсканируйте код телефоном: Telegram -> Настройки -> Устройства -> Подключить устройство.")
        print(f"Если код не сканируется, откройте ссылку на устройстве, где уже есть Telegram:\n  {url}")
    else:
        print("Эта консоль не умеет рисовать QR-код.")
        print(f"Откройте ссылку на устройстве, где уже есть Telegram:\n  {url}")
    print("Жду сканирования...")


async def _qr_login(client) -> bool:
    """Log in by QR. Returns ``False`` if nobody scanned any of the codes."""
    qr = await client.qr_login()
    _show_qr(qr.url)
    for _ in range(QR_MAX_REFRESHES):
        try:
            # Without a timeout Telethon waits exactly until this code expires.
            await qr.wait()
            return True
        except asyncio.TimeoutError:
            await qr.recreate()
            print("\nКод устарел, вот новый.")
            _show_qr(qr.url)
        except errors.SessionPasswordNeededError:
            await _enter_password(client)
            return True
    return False


# ---------------------------------------------------------------------------
# Phone number
# ---------------------------------------------------------------------------


async def _phone_login(client) -> None:
    print()
    print("Вход по номеру телефона.")
    phone = ""
    for attempt in range(PHONE_ATTEMPTS):
        phone = input("Номер телефона в международном формате (например, +79991234567): ").strip()
        try:
            await client.send_code_request(phone)
            break
        except errors.PhoneNumberInvalidError:
            if attempt == PHONE_ATTEMPTS - 1:
                raise LoginFailed("Telegram не принял номер телефона.") from None
            print("Telegram не принял этот номер. Проверьте его и введите ещё раз.")
        except errors.PhoneNumberBannedError:
            raise LoginFailed("Этот номер заблокирован в Telegram.") from None
        except errors.FloodWaitError as exc:
            raise LoginFailed(_flood_message(exc)) from None

    print("Telegram прислал код: сообщением в приложение Telegram или, если его нигде нет, по SMS.")
    for attempt in range(PHONE_ATTEMPTS):
        code = input("Код из сообщения: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
            return
        except errors.PhoneCodeInvalidError:
            if attempt == PHONE_ATTEMPTS - 1:
                raise LoginFailed("Код не подошёл.") from None
            print("Код не подошёл. Введите его ещё раз.")
        except errors.PhoneCodeExpiredError:
            raise LoginFailed("Код устарел. Запустите вход заново.") from None
        except errors.FloodWaitError as exc:
            raise LoginFailed(_flood_message(exc)) from None
        except errors.SessionPasswordNeededError:
            await _enter_password(client)
            return


def _flood_message(exc: errors.FloodWaitError) -> str:
    minutes = max(1, (exc.seconds + 59) // 60)
    return f"Слишком много попыток входа. Telegram просит подождать {minutes} мин. и попробовать снова."


# ---------------------------------------------------------------------------
# The cloud password (2FA)
# ---------------------------------------------------------------------------


async def _enter_password(client) -> None:
    print()
    print("У аккаунта включён облачный пароль (двухэтапная аутентификация).")
    while True:
        # getpass reads the terminal itself and echoes nothing.
        password = getpass.getpass("Облачный пароль (символы не отображаются): ")
        try:
            await client.sign_in(password=password)
            return
        except errors.PasswordHashInvalidError:
            print("Пароль не подошёл, попробуйте ещё раз.")
        except errors.FloodWaitError as exc:
            raise LoginFailed(_flood_message(exc)) from None
        finally:
            del password


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _login(api_id: int, api_hash: str, use_qr: bool) -> str:
    """Run the login and return the session string."""
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        authorized = False
        if use_qr:
            try:
                authorized = await _qr_login(client)
            except (LoginFailed, errors.ApiIdInvalidError):
                raise
            except Exception as exc:
                print(f"\nВход по QR-коду не удался: {exc}")
            if not authorized:
                print("Попробуем войти по номеру телефона.")
        if not authorized:
            await _phone_login(client)
        # StringSession.save() is an unbound method that works on any Session.
        return StringSession.save(client.session)
    finally:
        await client.disconnect()


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="telegram-mcp-ag-login",
        description="Вход в Telegram для telegram-mcp-ag: сессия сохраняется в config.env.",
    )
    parser.add_argument(
        "--phone",
        action="store_true",
        help="Сразу входить по номеру телефона, без QR-кода.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> None:
    args = _parse_args(argv)

    try:
        _config.load(_config.CONFIG_PATH)
    except _config.MissingSessionError:
        pass
    except _config.ConfigError as exc:
        print(f"Не найдены api_id/api_hash: {exc}", file=sys.stderr)
        sys.exit(1)

    api_id = int(os.environ["TELEGRAM_API_ID"].strip())
    api_hash = os.environ["TELEGRAM_API_HASH"].strip()

    try:
        session_string = asyncio.run(_login(api_id, api_hash, use_qr=not args.phone))
    except errors.ApiIdInvalidError:
        print(f"\n{_BAD_CREDENTIALS_MESSAGE}", file=sys.stderr)
        sys.exit(1)
    except LoginFailed as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nВход прерван.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\nНе удалось войти в Telegram: {exc}", file=sys.stderr)
        sys.exit(1)

    _config.write_values(
        {
            "TELEGRAM_API_ID": str(api_id),
            "TELEGRAM_API_HASH": api_hash,
            "TELEGRAM_SESSION_STRING": session_string,
        },
        _config.CONFIG_PATH,
    )
    print(f"\nВход выполнен. Сессия сохранена в {_config.CONFIG_PATH}.")


if __name__ == "__main__":
    main()
