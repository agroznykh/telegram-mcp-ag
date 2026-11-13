"""Which account a tool call goes to: the only one, the default, or a question.

Upstream's ``with_account`` answers "no account given" with a fan-out to every
account (reads) or an error (writes). Here nothing is ever asked of all accounts
at once: without ``account`` the call goes to the sole account or the default
one, and when there is neither the tool answers with a question the assistant
relays to the user.

The decision is taken at call time from ``runtime.clients`` -- the server's
source of truth about accounts (it already accounts for pool/``_NAME`` keys and
for the keys ``config.load`` threw out), and in login mode the clients are
replaced after import.
"""

import os
from contextvars import ContextVar
from functools import wraps
from typing import Callable, Iterable, Optional

from telegram_mcp import runtime as _runtime
from telegram_mcp.account_labels import canonical_account_label

DEFAULT_ACCOUNT_ENV = "TELEGRAM_DEFAULT_ACCOUNT"

# Set on every function this module returns; the startup check looks for it.
MARK = "_telegram_mcp_ag_account_aware"

# Label of the account the running call goes to. Read by the phases that connect
# an account lazily and word failures per account.
current_account: ContextVar[Optional[str]] = ContextVar("telegram_mcp_ag_account", default=None)


def _labels() -> list[str]:
    return list(_runtime.clients)


def _labels_text(labels: Iterable[str]) -> str:
    return ", ".join(f"`{label}`" for label in labels)


def default_label() -> Optional[str]:
    """The configured default account, if it is one of the running accounts.

    A blank value is "not set". A label that names no running account is ignored
    (``config.load`` has already warned about it), so a stale default can never
    send calls to an account that does not exist.
    """
    value = os.environ.get(DEFAULT_ACCOUNT_ENV, "").strip()
    if not value:
        return None
    label = canonical_account_label(value)
    return label if label in _runtime.clients else None


def resolve(account: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(label, None)`` for the account to use or ``(None, text)`` to answer with."""
    labels = _labels()
    if account is not None and str(account).strip() != "":
        label = canonical_account_label(str(account))
        if label in _runtime.clients:
            return label, None
        return None, f"Нет аккаунта `{account}`. Есть: {_labels_text(labels)}."
    if len(labels) <= 1:
        return (labels[0] if labels else None), None
    default = default_label()
    if default is not None:
        return default, None
    return None, (
        f"Укажите аккаунт (параметр account): {_labels_text(labels)}. "
        "Если из просьбы непонятно, какой — спросите пользователя."
    )


def _account_aware(fn: Callable) -> Callable:
    setattr(fn, MARK, True)
    return fn


def with_account(readonly: bool = False):
    """Drop-in for upstream's ``with_account``; ``readonly`` is kept for the signature.

    The wrapped function must take ``account`` as a keyword (it is how FastMCP
    passes it) and is always called with the canonical label of one account.
    With a single account that is the only account, so nothing changes for a
    user who has one. A text instead of a call is a normal tool answer: every
    tool this wraps returns ``str``.
    """

    def decorator(fn: Callable):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            label, answer = resolve(kwargs.get("account"))
            if answer is not None:
                return answer
            if label is not None:
                kwargs["account"] = label
            token = current_account.set(label)
            try:
                return await fn(*args, **kwargs)
            finally:
                current_account.reset(token)

        return _account_aware(wrapper)

    return decorator


def wrap_tool(tool) -> None:
    """Put an already registered tool behind :func:`with_account`.

    For upstream tools that take ``account`` but are not decorated by upstream
    (the wait tools). FastMCP computed the schema at registration, so replacing
    ``fn`` changes the behaviour and not the parameters.
    """
    if getattr(tool.fn, MARK, False):
        return
    tool.fn = with_account(readonly=True)(tool.fn)


def unwrapped_tools(tools: Iterable) -> list[str]:
    """Names of tools that take ``account`` but are not behind :func:`with_account`."""
    missing = []
    for tool in tools:
        if "account" not in (tool.parameters or {}).get("properties", {}):
            continue
        if not getattr(tool.fn, MARK, False):
            missing.append(tool.name)
    return sorted(missing)


def ensure_all_wrapped(tools: Iterable) -> None:
    """Refuse to start if an ``account`` tool could still fan out to every account.

    Protects against an upstream bump quietly adding a tool (or dropping a
    decorator) that polls all accounts without being asked.
    """
    missing = unwrapped_tools(tools)
    if missing:
        raise RuntimeError(
            "telegram-mcp-ag: these tools take `account` but are not behind the "
            "account selection and could query every account at once: "
            + ", ".join(missing)
        )

