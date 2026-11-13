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

# How long a call waits for the connection. Well below upstream's 55 s limit on a
# whole tool call (TELEGRAM_TOOL_TIMEOUT_SECONDS): the lock wait (up to 20 s) and
# the duplicate retries (2+4+8 s) can add up to more than that on a bad day.
# The connection itself is not cut off; the next call joins it.
CONNECT_WAIT_SECONDS = 40.0

# A transient failure (network, someone else holds the session lock) is not tried
# again sooner than this, so a broken network is not hammered with every call.
RETRY_AFTER_SECONDS = 60.0

# The session is gone for good: nothing a retry could change.
_FINAL_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    AuthKeyInvalidError,
)


@dataclass
class Failure:
    error: BaseException
    at: float
    final: bool


_ready: set[str] = set()
_tasks: dict[str, "asyncio.Task[None]"] = {}
_failures: dict[str, Failure] = {}
# Strong references: the loop only holds tasks weakly.
_background: set["asyncio.Task[None]"] = set()


def reset() -> None:
    """Forget all state. For tests."""
    _ready.clear()
    _tasks.clear()
    _failures.clear()
    _background.clear()


def mark_ready(label: str) -> None:
    """The account is connected and authorized already (QR login in the chat)."""
    _ready.add(label)
    _failures.pop(label, None)


def is_ready(label: str) -> bool:
    return label in _ready


def failure(label: str) -> Optional[Failure]:
    return _failures.get(label)


def _is_final(error: BaseException) -> bool:
    from telegram_mcp import runner

    if isinstance(error, runner.SessionLockError):
        # Another copy of the server holds the session right now: it may let go.
        return False
    if isinstance(error, (runner._NotAuthorized, *_FINAL_ERRORS)):
        return True
    # What is left of RuntimeError is the "logged in to a different account" check.
    return isinstance(error, RuntimeError)


def _one_line(value: object, limit: int = 200) -> str:
    text = " ".join(str(value).split()).replace("`", "'")
    return text if len(text) <= limit else text[:limit] + "…"


def _quoted(label: str) -> str:
    return _one_line(label, 40)


def reason(error: BaseException) -> str:
    """A short cause for a failed connection. Wording per cause comes in a later phase."""
    detail = _one_line(error)
    name = type(error).__name__
    return f"{name}: {detail}" if detail else name


def unavailable_text(label: str, state: Failure) -> str:
    return f"Аккаунт `{_quoted(label)}` не подключился: {reason(state.error)}"


def _warm_up(label: str, client) -> None:
    """Fill the entity cache of one just connected account, once, in the background."""

    async def _run() -> None:
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
        _failures[label] = Failure(exc, time.monotonic(), _is_final(exc))
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
    return f"Аккаунт `{_quoted(label)}` не подключился."


async def ensure_all_ready() -> dict[str, str]:
    """Connect every account in parallel; return label -> text for those that failed."""
    labels = list(_runtime.clients)
    answers = await asyncio.gather(*(ensure_account_ready(label) for label in labels))
    return {label: answer for label, answer in zip(labels, answers) if answer is not None}
