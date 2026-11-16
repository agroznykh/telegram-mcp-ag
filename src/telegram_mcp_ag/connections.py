"""Connecting accounts lazily: an account is connected by the first call that needs it.

Upstream's runner connects every account at startup and exits when one cannot be
reached. Here nothing is connected at start (see ``server.py``); the first call
for an account connects that account alone, through upstream's own
``_connect_authorized_client`` -- it takes the session lock, retries a transient
``AUTH_KEY_DUPLICATED`` and checks authorization. Upstream's ``ensure_connected``
would also connect, but without the lock, so ours always goes first.

Per account there is one connecting task. Every call that arrives meanwhile waits
on the same task through ``asyncio.shield``: a timeout or a cancellation of one
call does not break the connection the others are waiting for. A failure is
remembered per account; the other accounts are not touched and the process keeps
running. Upstream's private names are looked up at call time (``runner`` is
imported lazily: it must not be imported before ``server.py`` has put our
``with_account`` in place).
"""

import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Optional

from telegram_mcp import runtime as _runtime
from telethon.errors.rpcerrorlist import (
    AuthKeyDuplicatedError,
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
)

from telegram_mcp_ag import config as _config
from telegram_mcp_ag import relogin as _relogin

# How long a call waits for the connection. Well below upstream's 55 s limit on a
# whole tool call (TELEGRAM_TOOL_TIMEOUT_SECONDS): the lock wait (up to 20 s) and
# the duplicate retries (2+4+8 s) can add up to more than that on a bad day.
# The connection itself is not cut off; the next call joins it.
CONNECT_WAIT_SECONDS = 40.0

# A transient failure (network, someone else holds the session lock) is not tried
# again sooner than this, so a broken network is not hammered with every call.
RETRY_AFTER_SECONDS = 60.0

# Why an account is unavailable. The first three end it for good (until a fresh
# login and a restart of the client): the session is revoked, used from two
# machines at once, or logged in to a different account than expected. Everything
# else (network, a timeout, another copy of the server holding the session lock)
# passes by itself.
REVOKED = "revoked"
DUPLICATE = "duplicate"
WRONG_ACCOUNT = "wrong_account"
TRANSIENT = "transient"

# What Telegram answers for a session that is gone for good: revoked in
# Settings -> Devices, or all sessions ended at once.
_REVOKED_ERRORS = (AuthKeyUnregisteredError, SessionRevokedError, AuthKeyInvalidError)


@dataclass
class Failure:
    error: BaseException
    at: float
    kind: str = TRANSIENT

    @property
    def final(self) -> bool:
        return self.kind != TRANSIENT


_ready: set[str] = set()
_tasks: dict[str, "asyncio.Task[None]"] = {}
_failures: dict[str, Failure] = {}
# Name and @username of accounts that have connected, taken once after connecting:
# list_accounts shows them without asking Telegram anything.
_profiles: dict[str, str] = {}
# Strong references: the loop only holds tasks weakly.
_background: set["asyncio.Task[None]"] = set()


def mark_ready(label: str) -> None:
    """The account is connected and authorized already (QR login in the chat)."""
    _ready.add(label)
    _failures.pop(label, None)


def failure(label: str) -> Optional[Failure]:
    return _failures.get(label)


def is_ready(label: str) -> bool:
    return label in _ready


def session_error_kind(error: BaseException) -> Optional[str]:
    """``REVOKED`` / ``DUPLICATE`` for an error that says the session is gone, else ``None``.

    These are what a tool raises in the middle of work. At connection time
    :func:`_connect_error_kind` also knows upstream's own exceptions.
    """
    if isinstance(error, AuthKeyDuplicatedError):
        return DUPLICATE
    if isinstance(error, _REVOKED_ERRORS):
        return REVOKED
    return None


def _connect_error_kind(error: BaseException) -> str:
    from telegram_mcp import runner

    if isinstance(error, runner.SessionLockError):
        # Another copy of the server holds the session right now: it may let go.
        return TRANSIENT
    kind = session_error_kind(error)
    if kind is not None:
        return kind
    # A revoked session does not surface as an RPC error here: Telethon's
    # is_user_authorized() swallows it and upstream raises _NotAuthorized.
    if isinstance(error, runner._NotAuthorized):
        return REVOKED
    # What is left of RuntimeError is the "logged in to a different account" check.
    if isinstance(error, RuntimeError):
        return WRONG_ACCOUNT
    return TRANSIENT


def _one_line(value: object, limit: int = 200) -> str:
    text = " ".join(str(value).split()).replace("`", "'")
    return text if len(text) <= limit else text[:limit] + "…"


def _quoted(label: str) -> str:
    return _one_line(label, 40)


def is_single() -> bool:
    """Whether the server has exactly one account (read at call time)."""
    return len(_runtime.clients) == 1


def label_for_call() -> Optional[str]:
    """The account of the running call, or the only account; ``None`` if unknown."""
    from telegram_mcp_ag import accounts as _accounts

    label = _accounts.current_account.get()
    if label is not None:
        return label
    labels = list(_runtime.clients)
    return labels[0] if len(labels) == 1 else None


def _single_text() -> str:
    """The session of the only account is gone and has been deleted: QR in the chat."""
    return (
        "Сессия Telegram для этого аккаунта отозвана (устройство удалено в "
        "Telegram -> Настройки -> Устройства, разлогинены все сеансы разом, либо "
        "одна и та же сессия использовалась на двух компьютерах). "
        f"Сброшенную сессию уже удалили -- {_relogin.RESTART_CLIENT}, и попросите "
        "ассистента подключить Telegram заново: сервер покажет новый QR-код "
        "прямо в чате, терминал не нужен. Если это "
        f"неудобно и есть терминал -- альтернатива: {_relogin.instructions()}"
    )


_CAUSE = {
    REVOKED: (
        "отозвана (устройство удалено в Telegram -> Настройки -> Устройства, либо "
        "разлогинены все сеансы разом)"
    ),
    DUPLICATE: (
        "сброшена Telegram: одна и та же сессия использовалась на двух компьютерах "
        "одновременно"
    ),
    WRONG_ACCOUNT: "относится к другому аккаунту, чем ожидалось",
}


def lost_text(label: str, kind: str) -> str:
    """What to answer when the session of ``label`` is gone for good (``kind`` is not transient).

    One account: the dead session has been deleted, the way back is a restart
    and a QR login in the chat. Several: only this account is named, with the
    command that logs in to it alone; the other sessions are not touched.
    """
    if is_single() and kind != WRONG_ACCOUNT:
        return _single_text()
    try:
        command = _relogin.instructions(label)
    except _config.ConfigError:
        # Cannot happen for a label that came from runtime.clients; never paste an unchecked one.
        command = _relogin.instructions()
    others = "" if is_single() else " Остальные аккаунты работают, их сессии не тронуты."
    return (
        f"Сессия Telegram для аккаунта `{_quoted(label)}` {_CAUSE[kind]}.{others} "
        f"Войти заново в этот аккаунт: {command}. После этого {_relogin.RESTART_CLIENT}."
    )


def reason(state: "Failure") -> str:
    """A cause a person can act on. The exception itself goes to stderr only."""
    return "не удалось связаться с Telegram. Повторите запрос чуть позже."


def unavailable_text(label: str, state: Failure) -> str:
    if state.final:
        return lost_text(label, state.kind)
    return f"Аккаунт `{_quoted(label)}` не подключился: {reason(state)}"


def _forget_dead_session(label: str, kind: str) -> None:
    """One account only: delete its dead session so the next start offers a QR login.

    With several accounts nothing is deleted: the user logs in to the lost one
    with the command in the answer, and the others must stay as they are.
    """
    if not is_single() or kind == WRONG_ACCOUNT:
        return
    try:
        _config.clear_session(label=label)
    except (OSError, _config.ConfigError):
        pass


def mark_lost(label: str, error: BaseException, kind: str) -> None:
    """Record that the account dropped out (in the middle of work or on connecting).

    Later calls to it get the same answer without going to Telegram.
    """
    _ready.discard(label)
    _failures[label] = Failure(error, time.monotonic(), kind)
    _forget_dead_session(label, kind)


def profile(label: str) -> Optional[str]:
    return _profiles.get(label)


def _describe(me) -> str:
    """Name and @username, no phone. Both are the user's own text: sanitized like upstream does."""
    name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "без имени"
    text = _runtime.sanitize_name(name)
    username = getattr(me, "username", None)
    if username:
        text += f" (@{_runtime.sanitize_name(username)})"
    return text


def _warm_up(label: str, client) -> None:
    """Fill the entity cache of one just connected account, once, in the background."""

    async def _run() -> None:
        try:
            me = await client.get_me()
            if me is not None:
                _profiles[label] = _describe(me)
        except Exception as exc:
            print(f"Profile of '{label}' not fetched: {exc}", file=sys.stderr)
        try:
            await client.get_dialogs()
        except Exception as exc:
            print(f"Entity cache warm failed for '{label}': {exc}", file=sys.stderr)

    task = asyncio.ensure_future(_run())
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _release(label: str, client) -> None:
    """Undo a half-done connection: free the session lock and close the socket.

    Upstream leaves the lock to ``_connect_or_skip``, which we have switched off.
    Without this a retry would overwrite the registered lock (a leaked handle) and
    with TELEGRAM_SESSION_LOCK=exclusive run into our own lock and fail after 20 s.
    """
    from telegram_mcp import runner

    lock = runner._session_locks.pop(label, None)
    if lock is not None:
        try:
            lock.release()
        except Exception as exc:
            print(f"Releasing the session lock of '{label}' failed: {exc}", file=sys.stderr)
    try:
        await client.disconnect()
    except Exception:
        pass


async def _connect(label: str, client) -> None:
    """The one connecting task of an account. Never raises: it records the outcome."""
    from telegram_mcp import runner

    try:
        await runner._connect_authorized_client(label, client)
    except asyncio.CancelledError:
        await _release(label, client)
        raise
    except Exception as exc:
        print(f"Account '{label}' did not connect: {type(exc).__name__}: {exc}", file=sys.stderr)
        await _release(label, client)
        if isinstance(exc, runner._NotAuthorized) and label in _runtime.OPTIONAL_ACCOUNTS:
            # What upstream's _connect_or_skip did at startup: a TELEGRAM_SESSION_NAME
            # session that never logged in, next to labelled accounts, is not an account.
            _runtime.clients.pop(label, None)
            print(f"Skipping account '{label}': its session is not logged in.", file=sys.stderr)
            return
        kind = _connect_error_kind(exc)
        _failures[label] = Failure(exc, time.monotonic(), kind)
        _forget_dead_session(label, kind)
        return
    _failures.pop(label, None)
    _ready.add(label)
    _warm_up(label, client)


async def ensure_account_ready(label: str) -> Optional[str]:
    """Connect the account if it is not yet; return ``None`` when it is ready.

    Otherwise returns the text to answer the tool call with -- the account is
    unavailable, or still connecting after :data:`CONNECT_WAIT_SECONDS`.
    """
    if label in _ready:
        return None
    client = _runtime.clients.get(label)
    if client is None:
        return None  # the account selection has answered about it already

    state = _failures.get(label)
    if state is not None:
        if state.final or time.monotonic() - state.at < RETRY_AFTER_SECONDS:
            return unavailable_text(label, state)
        del _failures[label]

    task = _tasks.get(label)
    if task is None or task.done():
        task = asyncio.ensure_future(_connect(label, client))
        _tasks[label] = task
        task.add_done_callback(
            lambda done, label=label: _tasks.pop(label, None) if _tasks.get(label) is done else None
        )

    try:
        await asyncio.wait_for(asyncio.shield(task), CONNECT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        return (
            f"Аккаунт `{_quoted(label)}` подключается дольше обычного. "
            "Повторите запрос через минуту."
        )
    if label in _ready:
        return None
    state = _failures.get(label)
    if state is not None:
        return unavailable_text(label, state)
    if label not in _runtime.clients:
        return (
            f"Аккаунта `{_quoted(label)}` нет: его сессия не входила в Telegram, аккаунт "
            "пропущен. Повторите запрос."
        )
    return f"Аккаунт `{_quoted(label)}` не подключился."


async def ensure_all_ready() -> dict[str, str]:
    """Connect every account in parallel; return label -> text for those that failed."""
    labels = list(_runtime.clients)
    answers = await asyncio.gather(*(ensure_account_ready(label) for label in labels))
    return {label: answer for label, answer in zip(labels, answers) if answer is not None}
