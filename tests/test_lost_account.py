"""An account that drops out: what is answered, what is deleted, what keeps working.

Every check runs in a fresh interpreter with made-up sessions (importing the
server registers tools on a process-wide MCP instance). Upstream's connection and
the tools' ``get_client`` are replaced by stand-ins, so nothing touches the network.
"""

import json
import os
import subprocess
import sys

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession


def _session() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=os.urandom(256))
    return session.save()


def _run(script: str, accounts=("WORK", "PERSONAL"), **extra_env: str):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env.update(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="test-hash")
    for label in accounts:
        key = "TELEGRAM_SESSION_STRING" if label == "" else f"TELEGRAM_SESSION_STRING_{label}"
        env[key] = _session()
    env.update(extra_env)
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


# `config_path` is a copy of the sessions in a throwaway config.env: what the
# server deletes it deletes there. `boom[label]` is what the tool raises for that
# account on use; `connect_error[label]` is what connecting raises.
_PREAMBLE = """
import asyncio, json, os, platform, tempfile
from pathlib import Path
from telethon.errors.rpcerrorlist import (
    AuthKeyDuplicatedError, AuthKeyInvalidError, AuthKeyUnregisteredError, SessionRevokedError,
)
from types import SimpleNamespace
from telegram_mcp_ag import server, connections, accounts
from telegram_mcp import runtime, runner
from telegram_mcp.tools import chats
runtime._apply_exposed_tools_mode()

config_path = Path(tempfile.mkdtemp()) / "config.env"
server._config.CONFIG_PATH = config_path
lines = ["TELEGRAM_API_ID=1", "TELEGRAM_API_HASH=h"]
for key, value in os.environ.items():
    if key.startswith("TELEGRAM_SESSION_STRING"):
        lines.append(f"{key}={value}")
config_path.write_text("\\n".join(lines) + "\\n")

def rpc(cls):
    return cls(request=SimpleNamespace())

connect_error = {}
class FakeLock:
    def release(self): pass
async def fake_connect(label, client):
    runner._session_locks[label] = FakeLock()
    if label in connect_error:
        raise connect_error[label]
runner._connect_authorized_client = fake_connect
for _c in runtime.clients.values():
    async def _noop(): pass
    async def _dialogs(): return []
    _c.disconnect = _noop
    _c.get_dialogs = _dialogs

boom = {}
reached = []
def fake_get_client(account=None):
    reached.append(account)
    if account in boom:
        raise boom[account]
    return "client"
chats.get_client = fake_get_client

def tool(name):
    return runtime.mcp._tool_manager._tools[name]

def keys():
    return sorted(l.split("=")[0] for l in config_path.read_text().split() if l.startswith("TELEGRAM_SESSION"))
"""
_ERRORS = ["AuthKeyUnregisteredError", "SessionRevokedError", "AuthKeyInvalidError", "AuthKeyDuplicatedError"]


@pytest.mark.parametrize("error", _ERRORS)
def test_several_accounts_a_session_lost_in_the_middle_names_the_account_and_deletes_nothing(error):
    script = (
        _PREAMBLE
        + f"""
boom["work"] = rpc({error})
async def main():
    first = await tool("get_chats").fn(account="work")
    reached_after_first = list(reached)
    second = await tool("get_chats").fn(account="work")
    personal = await tool("get_chats").fn(account="personal")
    return first, second, personal, reached_after_first
first, second, personal, after_first = asyncio.run(main())
print(json.dumps({{"first": first, "second": second, "personal": personal,
    "after_first": after_first, "reached": reached, "keys": keys()}}))
"""
    )
    result = _run(script)
    assert "`work`" in result["first"]
    assert "--account work" in result["first"] or "-Account work" in result["first"]
    assert "перезапустите" in result["first"]
    # nothing deleted, the other account is untouched and still answers
    assert result["keys"] == ["TELEGRAM_SESSION_STRING_PERSONAL", "TELEGRAM_SESSION_STRING_WORK"]
    assert result["personal"] != result["first"] and "`work`" not in result["personal"]
    # the next call to the lost account is answered without reaching the tool
    assert result["second"] == result["first"]
    assert result["reached"] == ["work", "personal"]


def test_one_account_a_session_lost_in_the_middle_is_deleted_and_the_qr_is_offered():
    script = (
        _PREAMBLE
        + """
boom["default"] = rpc(AuthKeyUnregisteredError)
answer = asyncio.run(tool("get_chats").fn())
print(json.dumps({"answer": answer, "keys": keys()}))
"""
    )
    result = _run(script, accounts=("",))
    assert "QR" in result["answer"] and "уже удалили" in result["answer"]
    assert result["keys"] == []


def test_one_labelled_account_deletes_only_its_own_key():
    script = (
        _PREAMBLE
        + """
boom["work"] = rpc(AuthKeyDuplicatedError)
answer = asyncio.run(tool("get_chats").fn())
print(json.dumps({"answer": answer, "keys": keys()}))
"""
    )
    result = _run(script, accounts=("WORK",))
    assert "QR" in result["answer"]
    assert result["keys"] == []


@pytest.mark.parametrize("label", ["work", "personal"])
def test_several_accounts_a_session_revoked_on_connecting_keeps_every_key(label):
    script = (
        _PREAMBLE
        + f"""
connect_error["{label}"] = runner._NotAuthorized("never logged in")
async def main():
    lost = await tool("get_chats").fn(account="{label}")
    return lost
lost = asyncio.run(main())
print(json.dumps({{"lost": lost, "keys": keys()}}))
"""
    )
    result = _run(script)
    assert f"--account {label}" in result["lost"] or f"-Account {label}" in result["lost"]
    assert len(result["keys"]) == 2


def test_one_account_a_session_revoked_on_connecting_is_deleted_and_asks_for_a_restart():
    script = (
        _PREAMBLE
        + """
connect_error["default"] = runner._NotAuthorized("never logged in")
answer = asyncio.run(tool("get_chats").fn())
print(json.dumps({"answer": answer, "keys": keys()}))
"""
    )
    result = _run(script, accounts=("",))
    assert "QR" in result["answer"] and "перезапустите" in result["answer"]
    assert result["keys"] == []


def test_a_session_of_another_account_names_the_command_and_is_not_deleted_even_for_one_account():
    script = (
        _PREAMBLE
        + """
connect_error["default"] = RuntimeError("Telegram client 'default' is logged in to a different account than expected")
answer = asyncio.run(tool("get_chats").fn())
print(json.dumps({"answer": answer, "keys": keys()}))
"""
    )
    result = _run(script, accounts=("",))
    assert "другому аккаунту" in result["answer"] and ("--relogin" in result["answer"] or "-Relogin" in result["answer"])
    assert result["keys"] == ["TELEGRAM_SESSION_STRING"]


def test_a_network_failure_says_try_later_and_has_no_command():
    script = (
        _PREAMBLE
        + """
connect_error["work"] = ConnectionError("no route")
answer = asyncio.run(tool("get_chats").fn(account="work"))
print(json.dumps({"answer": answer, "keys": keys()}))
"""
    )
    result = _run(script)
    assert "позже" in result["answer"] and "relogin" not in result["answer"].lower()
    assert len(result["keys"]) == 2


@pytest.mark.parametrize(
    ("system", "expected"),
    [("Darwin", "--relogin --account work"), ("Linux", "--relogin --account work"), ("Windows", "-Relogin -Account work")],
)
@pytest.mark.parametrize("kind", ["REVOKED", "DUPLICATE", "WRONG_ACCOUNT"])
def test_the_command_fits_the_os_for_every_cause(system, expected, kind):
    script = (
        _PREAMBLE
        + f"""
from telegram_mcp_ag import relogin
relogin.platform.system = lambda: "{system}"
print(json.dumps({{"text": connections.lost_text("work", connections.{kind})}}))
"""
    )
    text = _run(script)["text"]
    assert expected in text and "`work`" in text and "перезапустите" in text
    other = "-Relogin" if "--relogin" in expected else "--relogin"
    assert other not in text


def test_a_tool_without_an_account_among_several_does_not_guess():
    script = (
        _PREAMBLE
        + """
text = server._relogin_user_message(rpc(AuthKeyUnregisteredError))
print(json.dumps({"text": text, "keys": keys(), "failed": sorted(connections._failures)}))
"""
    )
    result = _run(script)
    assert "не удалось" in result["text"] and "--" not in result["text"]
    assert len(result["keys"]) == 2 and result["failed"] == []


def test_other_errors_are_not_taken_for_a_lost_session():
    script = (
        _PREAMBLE
        + """
print(json.dumps({"text": server._relogin_user_message(RuntimeError("x")),
                  "failed": sorted(connections._failures)}))
"""
    )
    result = _run(script)
    assert result["text"] is None and result["failed"] == []


# --- list_accounts ---------------------------------------------------------


def test_list_accounts_never_goes_to_the_network_or_connects():
    script = (
        _PREAMBLE
        + """
def forbidden(*a, **k):
    raise AssertionError("network")
for c in runtime.clients.values():
    c.get_me = forbidden
    c.connect = forbidden
text = asyncio.run(tool("list_accounts").fn())
print(json.dumps({"text": text, "connected": sorted(connections._ready)}))
"""
    )
    result = _run(script, TELEGRAM_DEFAULT_ACCOUNT="personal")
    lines = result["text"].splitlines()
    assert lines[0].startswith("personal (по умолчанию)") or lines[1].startswith("personal (по умолчанию)")
    assert all("ещё не подключался" in line for line in lines) and len(lines) == 2
    assert result["connected"] == []


def test_list_accounts_shows_name_and_username_without_phone_for_a_connected_account():
    script = (
        _PREAMBLE
        + """
me = SimpleNamespace(first_name="Ivan", last_name="P", username="ivan_p", phone="79990000000")
async def get_me(): return me
runtime.clients["work"].get_me = get_me
async def main():
    await tool("get_chats").fn(account="work")
    await asyncio.sleep(0.05)   # the profile is taken in the background
    return await tool("list_accounts").fn()
text = asyncio.run(main())
print(json.dumps({"text": text}))
"""
    )
    text = _run(script)["text"]
    work = next(line for line in text.splitlines() if line.startswith("work"))
    assert "подключён" in work and "Ivan P" in work and "@ivan_p" in work
    assert "7999" not in text
    assert "ещё не подключался" in next(line for line in text.splitlines() if line.startswith("personal"))


def test_list_accounts_says_why_an_account_dropped_out_and_how_to_return():
    script = (
        _PREAMBLE
        + """
boom["work"] = rpc(SessionRevokedError)
async def main():
    await tool("get_chats").fn(account="work")
    return await tool("list_accounts").fn()
print(json.dumps({"text": asyncio.run(main())}))
"""
    )
    text = _run(script)["text"]
    work = next(line for line in text.splitlines() if line.startswith("work"))
    assert "недоступен" in work and ("--account work" in text or "-Account work" in text)


def test_list_accounts_replaces_the_upstream_tool_without_changing_the_surface():
    script = (
        _PREAMBLE
        + """
t = tool("list_accounts")
print(json.dumps({"fn": t.fn.__module__, "ro": t.annotations.readOnlyHint,
                  "params": sorted(t.parameters.get("properties", {}))}))
"""
    )
    result = _run(script)
    assert result["fn"] == "telegram_mcp_ag.server" and result["ro"] is True
    assert result["params"] == []


def test_the_command_is_the_last_thing_in_the_answer_so_a_copy_gets_the_bare_label():
    script = (
        _PREAMBLE
        + """
boom["work"] = rpc(AuthKeyUnregisteredError)
print(json.dumps({"text": asyncio.run(tool("get_chats").fn(account="work"))}))
"""
    )
    assert _run(script)["text"].endswith(("--account work", "-Account work"))


def test_one_account_whose_session_cannot_be_deleted_is_not_told_it_was():
    # the session lives in the client's environment, not in config.env: nothing to delete
    script = (
        _PREAMBLE
        + """
config_path.write_text("TELEGRAM_API_ID=1\\nTELEGRAM_API_HASH=h\\n")
boom["default"] = rpc(AuthKeyUnregisteredError)
answer = asyncio.run(tool("get_chats").fn())
print(json.dumps({"answer": answer}))
"""
    )
    answer = _run(script, accounts=("",))["answer"]
    assert "уже удалили" not in answer and "QR" not in answer
    assert "--account default" in answer or "-Account default" in answer
    assert "остальных" not in answer


@pytest.mark.parametrize("error", ["SessionExpiredError", "AuthKeyPermEmptyError"])
def test_more_dead_session_errors_are_recognised_in_the_middle_of_work(error):
    from telethon.errors import rpcerrorlist  # noqa: F401

    script = (
        _PREAMBLE
        + f"""
from telethon.errors.rpcerrorlist import {error}
boom["work"] = rpc({error})
print(json.dumps({{"text": asyncio.run(tool("get_chats").fn(account="work"))}}))
"""
    )
    text = _run(script)["text"]
    assert "`work`" in text and ("--account work" in text or "-Account work" in text)


def test_an_unrelated_runtime_error_on_connecting_is_not_a_wrong_account():
    script = (
        _PREAMBLE
        + """
connect_error["work"] = RuntimeError("event loop is closed")
answer = asyncio.run(tool("get_chats").fn(account="work"))
print(json.dumps({"answer": answer, "final": connections.failure("work").final}))
"""
    )
    result = _run(script)
    assert result["final"] is False and "позже" in result["answer"]


def test_list_accounts_says_an_account_is_connecting_while_it_is():
    script = (
        _PREAMBLE
        + """
release = asyncio.Event()
async def slow_connect(label, client):
    await release.wait()
runner._connect_authorized_client = slow_connect
async def main():
    call = asyncio.ensure_future(tool("get_chats").fn(account="work"))
    await asyncio.sleep(0.05)
    text = await tool("list_accounts").fn()
    release.set()
    await call
    return text
print(json.dumps({"text": asyncio.run(main())}))
"""
    )
    work = next(l for l in _run(script)["text"].splitlines() if l.startswith("work"))
    assert "подключается сейчас" in work
