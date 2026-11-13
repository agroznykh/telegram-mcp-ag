"""Accounts are connected by the first call that needs them, one at a time.

Every check runs in a fresh interpreter with made-up sessions (importing the
server registers tools on a process-wide MCP instance). Upstream's
``_connect_authorized_client`` is replaced by a stand-in that counts and can
fail or wait, so nothing touches the network.
"""

import json
import os
import subprocess
import sys

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
    if accounts == ("",):
        env["TELEGRAM_SESSION_STRING"] = _session()
    else:
        for label in accounts:
            env[f"TELEGRAM_SESSION_STRING_{label}"] = _session()
    env.update(extra_env)
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


# `connect_calls` records each attempt; `behaviour[label]` decides what it does:
# "ok", an exception instance to raise, or an asyncio.Event to wait for first.
_PREAMBLE = """
import asyncio, json, time
from telegram_mcp_ag import server, connections
from telegram_mcp import runtime, runner
from telegram_mcp.tools import chats
runtime._apply_exposed_tools_mode()

connect_calls = []
behaviour = {}
released = []


class FakeLock:
    def __init__(self, label):
        self.label = label
    def release(self):
        released.append(self.label)


async def fake_connect(label, client):
    connect_calls.append(label)
    runner._session_locks[label] = FakeLock(label)
    action = behaviour.get(label, "ok")
    if isinstance(action, asyncio.Event):
        await action.wait()
    elif isinstance(action, BaseException):
        raise action


runner._connect_authorized_client = fake_connect

disconnected = []
for _label, _client in runtime.clients.items():
    async def _disconnect(_l=_label):
        disconnected.append(_l)
    async def _dialogs(_l=_label):
        return []
    _client.disconnect = _disconnect
    _client.get_dialogs = _dialogs

reached = []
def fake_get_client(account=None):
    reached.append(account)
    raise RuntimeError("stop")
chats.get_client = fake_get_client


def tool(name):
    return runtime.mcp._tool_manager._tools[name]
"""


def test_nothing_is_connected_at_startup_and_shutdown_cleans_up_all_clients():
    script = (
        _PREAMBLE
        + """
async def no_serve(transport):
    return None
runner._serve = no_serve

# A lock left registered by an earlier connection must be freed on the way out.
runner._session_locks["work"] = FakeLock("work")

async def main():
    await runner._main()

try:
    asyncio.run(main())
except SystemExit:
    pass
print(json.dumps({
    "connect_calls": connect_calls,
    "disconnected": sorted(disconnected),
    "released": released,
    "locks_left": list(runner._session_locks),
}))
"""
    )
    for accounts in (("WORK", "PERSONAL"), ("",)):
        result = _run(script, accounts=accounts)
        assert result["connect_calls"] == [], accounts
        assert result["disconnected"] == sorted(
            ["work", "personal"] if len(accounts) == 2 else ["default"]
        ), accounts
        assert result["locks_left"] == [], accounts
        assert result["released"] == ["work"], accounts


def test_first_call_connects_only_the_account_it_names():
    script = (
        _PREAMBLE
        + """
out = asyncio.run(tool("get_chats").fn(account="work"))
print(json.dumps({"connect_calls": connect_calls, "reached": reached}))
"""
    )
    result = _run(script)
    assert result["connect_calls"] == ["work"]
    assert result["reached"] == ["work"]


def test_parallel_calls_connect_the_account_once_and_later_ones_reuse_it():
    script = (
        _PREAMBLE
        + """
gate = asyncio.Event()
behaviour["work"] = gate

async def main():
    calls = [asyncio.ensure_future(connections.ensure_account_ready("work")) for _ in range(5)]
    await asyncio.sleep(0.05)
    gate.set()
    answers = await asyncio.gather(*calls)
    again = await connections.ensure_account_ready("work")
    return answers, again

answers, again = asyncio.run(main())
print(json.dumps({"connect_calls": connect_calls, "answers": answers, "again": again}))
"""
    )
    result = _run(script)
    assert result["connect_calls"] == ["work"]
    assert result["answers"] == [None] * 5 and result["again"] is None


def test_cancelling_a_call_does_not_break_the_connection_others_wait_for():
    script = (
        _PREAMBLE
        + """
gate = asyncio.Event()
behaviour["work"] = gate

async def main():
    first = asyncio.ensure_future(connections.ensure_account_ready("work"))
    second = asyncio.ensure_future(connections.ensure_account_ready("work"))
    await asyncio.sleep(0.05)
    first.cancel()
    await asyncio.sleep(0.05)
    gate.set()
    return await second, first.cancelled(), await connections.ensure_account_ready("work")

second, first_cancelled, again = asyncio.run(main())
print(json.dumps({"connect_calls": connect_calls, "second": second,
                  "first_cancelled": first_cancelled, "again": again, "released": released}))
"""
    )
    result = _run(script)
    assert result["first_cancelled"] is True
    assert result["connect_calls"] == ["work"]
    assert result["second"] is None and result["again"] is None
    assert result["released"] == []


def test_a_call_that_gives_up_waiting_leaves_the_connection_running():
    script = (
        _PREAMBLE
        + """
connections.CONNECT_WAIT_SECONDS = 0.05
gate = asyncio.Event()
behaviour["work"] = gate

async def main():
    slow = await connections.ensure_account_ready("work")
    gate.set()
    await asyncio.sleep(0.05)
    return slow, await connections.ensure_account_ready("work")

slow, later = asyncio.run(main())
print(json.dumps({"connect_calls": connect_calls, "slow": slow, "later": later}))
"""
    )
    result = _run(script)
    assert "`work`" in result["slow"] and "дольше обычного" in result["slow"]
    assert result["later"] is None
    assert result["connect_calls"] == ["work"]


def test_failed_account_answers_about_itself_frees_its_lock_and_spares_the_others():
    script = (
        _PREAMBLE
        + """
behaviour["work"] = runner._NotAuthorized("not authorized")

async def main():
    work = await tool("get_chats").fn(account="work")
    personal = await tool("get_chats").fn(account="personal")
    work_again = await tool("get_chats").fn(account="work")
    return work, personal, work_again

work, personal, work_again = asyncio.run(main())
print(json.dumps({
    "work": work, "personal": personal, "work_again": work_again,
    "connect_calls": connect_calls, "released": released, "disconnected": disconnected,
    "locks": sorted(runner._session_locks), "reached": reached,
}))
"""
    )
    result = _run(script)
    assert "`work`" in result["work"] and "не подключился" in result["work"]
    assert result["work_again"] == result["work"]
    # the revoked session is final: no second attempt
    assert result["connect_calls"] == ["work", "personal"]
    assert result["released"] == ["work"] and result["disconnected"] == ["work"]
    assert result["locks"] == ["personal"]
    assert result["reached"] == ["personal"]
    assert "не подключился" not in result["personal"] and "`work`" not in result["personal"]


def test_transient_failure_is_retried_only_after_the_pause():
    script = (
        _PREAMBLE
        + """
behaviour["work"] = ConnectionError("no route")

async def main():
    first = await connections.ensure_account_ready("work")
    soon = await connections.ensure_account_ready("work")
    attempts_before = len(connect_calls)
    behaviour["work"] = "ok"
    connections.RETRY_AFTER_SECONDS = 0
    later = await connections.ensure_account_ready("work")
    return first, soon, attempts_before, later

first, soon, attempts_before, later = asyncio.run(main())
print(json.dumps({"first": first, "soon": soon, "attempts_before": attempts_before,
                  "later": later, "connect_calls": connect_calls, "released": released}))
"""
    )
    result = _run(script)
    assert "`work`" in result["first"] and "позже" in result["first"]
    assert result["soon"] == result["first"]
    assert result["attempts_before"] == 1
    assert result["later"] is None and result["connect_calls"] == ["work", "work"]
    assert result["released"] == ["work"]  # the failed attempt's lock, not the good one


def test_session_lock_held_by_another_copy_is_not_final():
    script = (
        _PREAMBLE
        + """
behaviour["work"] = runner.SessionLockError("held")
asyncio.run(connections.ensure_account_ready("work"))
print(json.dumps({"final": connections.failure("work").final}))
"""
    )
    assert _run(script)["final"] is False


def test_client_that_logged_in_through_the_chat_is_not_connected_again():
    script = (
        _PREAMBLE
        + """
async def main():
    await server._activate_reading_tools()
    return await connections.ensure_account_ready("default")

answer = asyncio.run(main())
print(json.dumps({"answer": answer, "connect_calls": connect_calls}))
"""
    )
    result = _run(script, accounts=())
    assert result["answer"] is None and result["connect_calls"] == []


def test_enabling_the_incoming_feed_connects_all_accounts_and_names_the_failed():
    script = (
        _PREAMBLE
        + """
behaviour["work"] = runner._NotAuthorized("not authorized")
seen = []

async def original(settle_ms=6000):
    seen.append(settle_ms)
    return json.dumps({"enabled": True})

server._feed_original = original
out = asyncio.run(tool("enable_incoming_feed").fn(settle_ms=1000))
print(json.dumps({"out": json.loads(out), "seen": seen, "connect_calls": sorted(connect_calls)}))
"""
    )
    result = _run(script, TELEGRAM_EXPOSED_TOOLS="all")
    assert result["connect_calls"] == ["personal", "work"]
    assert result["seen"] == [1000]
    assert result["out"]["enabled"] is True
    assert list(result["out"]["unavailable_accounts"]) == ["work"]


def test_feed_with_every_account_up_returns_upstreams_answer_untouched():
    script = (
        _PREAMBLE
        + """
async def original(settle_ms=6000):
    return json.dumps({"enabled": True})

server._feed_original = original
print(json.dumps({"out": asyncio.run(tool("enable_incoming_feed").fn())}))
"""
    )
    result = _run(script, TELEGRAM_EXPOSED_TOOLS="all")
    assert json.loads(result["out"]) == {"enabled": True}


def test_upstream_names_this_package_relies_on_are_still_there():
    # `runner` is imported before the server: the server assigns `_connect_or_skip`
    # itself, which would hide an upstream rename.
    script = """
import inspect, json
from telegram_mcp import runtime, runner

missing = []
def need(ok, what):
    if not ok:
        missing.append(what)

need(callable(getattr(runner, "_connect_or_skip", None)), "runner._connect_or_skip")
need("_connect_or_skip" in inspect.getsource(runner._main), "runner._main calls _connect_or_skip")
connect = getattr(runner, "_connect_authorized_client", None)
need(callable(connect) and list(inspect.signature(connect).parameters) == ["label", "client"],
     "runner._connect_authorized_client(label, client)")
need(isinstance(getattr(runner, "_session_locks", None), dict), "runner._session_locks")
need(isinstance(getattr(runner, "_NotAuthorized", None), type), "runner._NotAuthorized")
need(isinstance(getattr(runner, "SessionLockError", None), type), "runner.SessionLockError")
need(runner.clients is runtime.clients, "runner.clients is runtime.clients")
need(isinstance(getattr(runtime, "clients", None), dict), "runtime.clients")
need(isinstance(getattr(runtime, "OPTIONAL_ACCOUNTS", None), set), "runtime.OPTIONAL_ACCOUNTS")
need(isinstance(getattr(runtime.mcp._tool_manager, "_tools", None), dict),
     "mcp._tool_manager._tools")
runtime._apply_exposed_tools_mode()
need("enable_incoming_feed" in runtime.mcp._tool_manager._tools, "tool enable_incoming_feed")
print(json.dumps({"missing": missing}))
"""
    result = _run(script, TELEGRAM_EXPOSED_TOOLS="all")
    assert result["missing"] == [], (
        "Upstream no longer has names the lazy connection relies on (connections.py, "
        "server.py): " + ", ".join(result["missing"]) + ". Read the upstream diff before "
        "bumping the SHA in pyproject.toml."
    )


def test_optional_account_that_never_logged_in_is_dropped_like_upstream_did():
    script = (
        _PREAMBLE
        + """
runtime.OPTIONAL_ACCOUNTS.add("work")
behaviour["work"] = runner._NotAuthorized("not authorized")
first = asyncio.run(connections.ensure_account_ready("work"))
print(json.dumps({"first": first, "labels": list(runtime.clients),
                  "failure": connections.failure("work") is not None}))
"""
    )
    result = _run(script)
    assert result["labels"] == ["personal"] and result["failure"] is False
    assert "`work`" in result["first"]
