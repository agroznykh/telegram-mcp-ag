import os
from types import SimpleNamespace

import pytest
from telethon.crypto import AuthKey
from telethon.errors.rpcerrorlist import (
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
)
from telethon.sessions import StringSession


def _dummy_session_string() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=os.urandom(256))
    return session.save()


os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("TELEGRAM_SESSION_STRING", _dummy_session_string())

from telegram_mcp_ag import server


def _rpc_error(cls):
    # telethon RPC error constructors take the originating request; its
    # content is irrelevant to the error type itself.
    return cls(request=SimpleNamespace())


@pytest.mark.parametrize("error_cls", [AuthKeyUnregisteredError, SessionRevokedError, AuthKeyInvalidError])
def test_relogin_user_message_covers_all_session_error_types(error_cls):
    message = server._relogin_user_message(_rpc_error(error_cls))
    assert message is not None
    assert "--relogin" in message or "-Relogin" in message


def test_relogin_user_message_ignores_unrelated_errors():
    assert server._relogin_user_message(ValueError("something else")) is None


@pytest.mark.parametrize(
    "system_name,expected_snippet",
    [
        ("Darwin", "--relogin"),
        ("Linux", "--relogin"),
        ("Windows", "-Relogin"),
    ],
)
def test_relogin_instructions_pick_the_right_command_for_a_known_os(monkeypatch, system_name, expected_snippet):
    monkeypatch.setattr(server.platform, "system", lambda: system_name)
    instructions = server._relogin_instructions()
    assert expected_snippet in instructions
    other_snippet = "-Relogin" if expected_snippet == "--relogin" else "--relogin"
    assert other_snippet not in instructions


def test_relogin_instructions_lists_both_commands_for_an_unknown_os(monkeypatch):
    monkeypatch.setattr(server.platform, "system", lambda: "FreeBSD")
    instructions = server._relogin_instructions()
    assert "--relogin" in instructions
    assert "-Relogin" in instructions


def test_log_and_format_error_fills_in_relogin_hint_for_session_errors():
    message = server.log_and_format_error("get_chats", _rpc_error(AuthKeyUnregisteredError))
    assert "--relogin" in message or "-Relogin" in message


def test_log_and_format_error_keeps_generic_message_for_other_errors():
    message = server.log_and_format_error("get_chats", ValueError("network down"))
    assert "--relogin" not in message and "-Relogin" not in message
    assert "error occurred" in message.lower()


def test_patch_reaches_every_wildcard_importing_tools_submodule():
    import importlib

    for name in (
        "accounts",
        "chats",
        "contacts",
        "events",
        "folders",
        "groups",
        "media",
        "messages",
        "profile",
    ):
        mod = importlib.import_module(f"telegram_mcp.tools.{name}")
        assert mod.log_and_format_error is server.log_and_format_error
