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
import unicodedata

import qrcode
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from telegram_mcp_ag import config as _config

# Each code lives about half a minute, so this is roughly five minutes of waiting.
QR_MAX_REFRESHES = 10
PHONE_ATTEMPTS = 3

# Not 1 (any other failure) or 2 (argparse already exits with 2 on a bad command
# line): the installers tell these cases apart by the code.
EXIT_BAD_CREDENTIALS = 3
EXIT_AMBIGUOUS_ACCOUNT = 4
EXIT_DECLINED = 5

_BAD_CREDENTIALS_MESSAGE = (
    "Telegram не принял api_id и api_hash. Проверьте их на my.telegram.org/apps "
    "-> API development tools: api_id -- число, api_hash -- длинная строка рядом "
    "с ним. Чаще всего их путают местами или копируют с лишним пробелом."
)


class LoginFailed(Exception):
    """A login that cannot go on. The message is shown to the user as is."""


class LoginDeclined(Exception):
    """The user said the account that logged in is not the one they meant."""


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


def _printable(text: str, stream=None) -> str:
    """``text`` made safe to show: no control characters (a display name could
    carry terminal escape sequences), nothing the console cannot encode (a legacy
    Windows code page would raise on an emoji, after the login had succeeded)."""
    stream = sys.stdout if stream is None else stream
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:
        return text.encode("ascii", errors="replace").decode("ascii")


def _describe_user(user) -> str:
    """Name and ``@username`` for the confirmation question -- never the phone."""
    name = " ".join(part for part in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if part)
    username = getattr(user, "username", None)
    parts = [name] if name else []
    if username:
        parts.append(f"@{username}")
    return _printable(" ".join(parts)) or "имя аккаунта неизвестно"


def _confirm_account(who: str) -> bool:
    """Ask whether this is the account the user meant. Enter means yes.

    The QR code is scanned in whichever account is active in the phone's app, so
    it is easy to log in a second time to the account that is already set up.
    """
    print()
    print(f"Вы вошли в аккаунт: {who}.")
    answer = input("Это нужный аккаунт? [Д/н]: ").strip().lower()
    return answer in ("", "д", "да", "y", "yes")


async def _login(api_id: int, api_hash: str, use_qr: bool, persist) -> None:
    """Run the login, confirm the account, then hand the session string to ``persist``.

    Saving happens in here, before the client is released, so that anything going
    wrong between the login and the saved key -- a declined account, Ctrl-C, a
    closed stdin, a file that cannot be written -- closes the session just
    created. Otherwise it would sit unused in the account's device list.
    """
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    saved = False
    authorized = False
    try:
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
            authorized = True
        try:
            who = _describe_user(await client.get_me())
        except Exception:
            who = "имя аккаунта узнать не удалось"
        if not _confirm_account(who):
            raise LoginDeclined()
        # StringSession.save() is an unbound method that works on any Session.
        persist(StringSession.save(client.session))
        saved = True
    finally:
        try:
            if authorized and not saved:
                try:
                    await client.log_out()
                except Exception:
                    pass
        finally:
            await client.disconnect()


def _label_type(allow_default: bool):
    """An argparse ``type=`` that checks a label and shows the reason if it is wrong."""

    def convert(text: str) -> str:
        try:
            return _config.validate_label(text, allow_default=allow_default)
        except _config.ConfigError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from None

    return convert


def _default_arg(text: str):
    if text.strip().lower() == "none":
        return None
    return _label_type(True)(text)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="telegram-mcp-ag-login",
        description="Вход в Telegram для telegram-mcp-ag.",
    )
    parser.add_argument(
        "--phone",
        action="store_true",
        help="Сразу входить по номеру телефона, без QR-кода.",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--account",
        type=_label_type(True),
        metavar="МЕТКА",
        help="Войти в аккаунт с этой меткой (или перелогиниться), не трогая остальные.",
    )
    modes.add_argument(
        "--rename-default",
        type=_label_type(False),
        metavar="НОВАЯ",
        help="Дать метку аккаунту без названия (default).",
    )
    modes.add_argument(
        "--set-default",
        type=_default_arg,
        metavar="МЕТКА|none",
        default=argparse.SUPPRESS,
        help="Аккаунт по умолчанию; none -- никакого, всегда спрашивать.",
    )
    modes.add_argument(
        "--list-accounts",
        action="store_true",
        help="Напечатать метки аккаунтов, по одной в строке (без сети и без секретов).",
    )
    args = parser.parse_args(argv)
    args.set_default_given = hasattr(args, "set_default")
    if args.phone and (args.rename_default or args.set_default_given or args.list_accounts):
        parser.error("--phone сочетается только с входом (--account или без него).")
    return args


def _run_list_accounts() -> None:
    env = _config.read_env(_config.CONFIG_PATH)
    default = _config.default_account(env)
    for label in _config.configured_labels(env):
        print(f"{label} (по умолчанию)" if label == default else label)


def _run_set_default(label) -> None:
    try:
        _config.set_default_account(label, _config.CONFIG_PATH)
    except _config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print("Аккаунт по умолчанию убран: теперь будем спрашивать." if label is None else f"Аккаунт по умолчанию: {label}.")


def _run_rename_default(new_label: str) -> None:
    try:
        _config.rename_session(_config.DEFAULT_LABEL, new_label, _config.CONFIG_PATH)
    except _config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"Аккаунт без названия теперь называется {new_label}.")


def _target_label(args, env: dict) -> str:
    """Which account the login is for; refuses to guess among several."""
    if args.account is not None:
        return args.account
    labels = _config.configured_labels(env)
    if len(labels) >= 2:
        print(
            "Аккаунтов несколько: "
            + ", ".join(labels)
            + ". Укажите, в какой входить: --account МЕТКА. Ничего не изменено.",
            file=sys.stderr,
        )
        sys.exit(EXIT_AMBIGUOUS_ACCOUNT)
    return labels[0] if labels else _config.DEFAULT_LABEL


def main(argv=None) -> None:
    args = _parse_args(argv)

    if args.list_accounts:
        _run_list_accounts()
        return
    if args.set_default_given:
        _run_set_default(args.set_default)
        return
    if args.rename_default:
        _run_rename_default(args.rename_default)
        return

    # Before load(): refusing to guess must not touch the file, and load() adds
    # default lines to it.
    env = _config.read_env(_config.CONFIG_PATH)
    label = _target_label(args, env)
    try:
        _config.load(_config.CONFIG_PATH)
    except _config.MissingSessionError:
        pass
    except _config.ConfigError:
        # Not the error text itself: it names the config file's location.
        print(
            "Не найдены api_id/api_hash. Запустите установщик ещё раз -- он их спросит.",
            file=sys.stderr,
        )
        sys.exit(1)

    api_id = int(os.environ["TELEGRAM_API_ID"].strip())
    api_hash = os.environ["TELEGRAM_API_HASH"].strip()

    def persist(session_string: str) -> None:
        # One write, only this account's key: a relogin replaces its dead
        # session, and the other accounts' sessions are none of its business.
        _config.write_session(
            label,
            session_string,
            _config.CONFIG_PATH,
            extra_values={"TELEGRAM_API_ID": str(api_id), "TELEGRAM_API_HASH": api_hash},
        )

    try:
        asyncio.run(_login(api_id, api_hash, not args.phone, persist))
    except errors.ApiIdInvalidError:
        # A distinct exit code so the installer knows to ask for them again.
        print(f"\n{_BAD_CREDENTIALS_MESSAGE}", file=sys.stderr)
        sys.exit(EXIT_BAD_CREDENTIALS)
    except LoginFailed as exc:
        print(f"\n{exc}", file=sys.stderr)
        sys.exit(1)
    except LoginDeclined:
        print("\nХорошо, ничего не сохранено. Запустите вход ещё раз и войдите в нужный аккаунт.", file=sys.stderr)
        sys.exit(EXIT_DECLINED)
    except (KeyboardInterrupt, EOFError):
        print("\nВход прерван.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\nНе удалось войти в Telegram: {exc}", file=sys.stderr)
        sys.exit(1)

    print("\nВход выполнен.")


if __name__ == "__main__":
    main()
