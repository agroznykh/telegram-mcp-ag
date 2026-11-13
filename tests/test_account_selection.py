"""Which account a call goes to: the only one, the default one, or a question.

Each check runs in a fresh interpreter with made-up sessions (importing the
server registers tools on a process-wide MCP instance). Tools are replaced by
recording stand-ins, so nothing touches the network.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

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


# Replaces get_client in the modules that hold the tools, so a call records the
# account it reached and stops there.
_PREAMBLE = """
import asyncio, json
from telegram_mcp_ag import server
from telegram_mcp import runtime
from telegram_mcp.tools import chats, events
runtime._apply_exposed_tools_mode()

reached = []
def fake_get_client(account=None):
    reached.append(account)
    raise RuntimeError("stop")
chats.get_client = fake_get_client
events.get_client = fake_get_client

def tool(name):
    return runtime.mcp._tool_manager._tools[name]

def call(name, **kwargs):
    reached.clear()
    out = asyncio.run(tool(name).fn(**kwargs))
    return {"out": out if isinstance(out, str) and "CHAT-ERR" not in out else None,
            "reached": list(reached)}
"""


def test_default_account_is_used_when_none_is_given():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats')))",
        TELEGRAM_DEFAULT_ACCOUNT="personal",
    )
    assert result["reached"] == ["personal"]


def test_changing_tool_ignores_the_default_and_asks_for_the_account():
    script = (
        _PREAMBLE
        + """
from telegram_mcp.tools import messages
messages.get_client = fake_get_client
print(json.dumps({
    "no_account": call('send_message', chat_id=1, message='hi'),
    "named": call('send_message', chat_id=1, message='hi', account='Work'),
}))
"""
    )
    result = _run(script, TELEGRAM_EXPOSED_TOOLS="all", TELEGRAM_DEFAULT_ACCOUNT="personal")
    assert result["no_account"]["reached"] == []
    assert "`work`" in result["no_account"]["out"]
    assert result["named"]["reached"] == ["work"]


def test_changing_tool_with_a_single_account_needs_no_account_argument():
    script = (
        _PREAMBLE
        + """
from telegram_mcp.tools import messages
messages.get_client = fake_get_client
print(json.dumps(call('send_message', chat_id=1, message='hi')))
"""
    )
    result = _run(script, accounts=("",), TELEGRAM_EXPOSED_TOOLS="all")
    assert result["reached"] == ["default"]


def test_default_account_spelling_is_folded_to_the_canonical_label():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats')))",
        TELEGRAM_DEFAULT_ACCOUNT=" Personal ",
    )
    assert result["reached"] == ["personal"]


def test_question_when_there_is_no_default_and_several_accounts():
    result = _run(_PREAMBLE + "print(json.dumps(call('get_chats')))")
    assert result["reached"] == []
    assert "`work`" in result["out"] and "`personal`" in result["out"]
    assert "account" in result["out"]


def test_blank_default_counts_as_not_set():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats')))", TELEGRAM_DEFAULT_ACCOUNT="  "
    )
    assert result["reached"] == [] and result["out"]


def test_default_naming_a_missing_account_is_ignored():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats')))", TELEGRAM_DEFAULT_ACCOUNT="ghost"
    )
    assert result["reached"] == [] and "`work`" in result["out"]


def test_explicit_account_wins_over_the_default():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats', account='Work')))",
        TELEGRAM_DEFAULT_ACCOUNT="personal",
    )
    assert result["reached"] == ["work"]


def test_unknown_account_gets_a_text_naming_the_real_ones():
    result = _run(_PREAMBLE + "print(json.dumps(call('get_chats', account='ghost')))")
    assert result["reached"] == []
    assert "`ghost`" in result["out"]
    assert "`work`" in result["out"] and "`personal`" in result["out"]


def test_single_account_needs_no_account_argument():
    result = _run(_PREAMBLE + "print(json.dumps(call('get_chats')))", accounts=("",))
    assert result["reached"] == ["default"]


def test_single_account_ignores_the_default_setting():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats')))",
        accounts=("",),
        TELEGRAM_DEFAULT_ACCOUNT="ghost",
    )
    assert result["reached"] == ["default"]


def test_wait_tools_follow_the_same_selection():
    script = (
        _PREAMBLE
        + """
res = {}
for name in ("wait_for_new_message", "wait_for_settled_message"):
    res[name] = call(name, chat_id="somebody")
print(json.dumps(res))
"""
    )
    asked = _run(script)
    for name in ("wait_for_new_message", "wait_for_settled_message"):
        assert asked[name]["reached"] == [], name
        assert "`work`" in asked[name]["out"], name

    defaulted = _run(script, TELEGRAM_DEFAULT_ACCOUNT="work")
    for name in ("wait_for_new_message", "wait_for_settled_message"):
        assert defaulted[name]["reached"] == ["work"], name


def test_every_account_tool_is_behind_the_selection_in_every_mode():
    script = """
import json
from telegram_mcp_ag import server, accounts
from telegram_mcp import runtime
runtime._apply_exposed_tools_mode()
tools = runtime.mcp._tool_manager.list_tools()
with_account = [t.name for t in tools if "account" in t.parameters.get("properties", {})]
print(json.dumps({"with_account": with_account, "missing": accounts.unwrapped_tools(tools)}))
"""
    for mode in ("read-only", "all"):
        result = _run(script, TELEGRAM_EXPOSED_TOOLS=mode)
        assert len(result["with_account"]) > 20, mode
        assert result["missing"] == [], mode


def test_login_mode_starts_with_the_check_in_place():
    # No sessions at all: the QR login server. Its single client is skipped by
    # the decorator, and the check still runs without complaint.
    script = """
import json
from telegram_mcp_ag import server
from telegram_mcp import runtime
runtime._apply_exposed_tools_mode()
print(json.dumps({"login": server._LOGIN_MODE}))
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env.update(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="test-hash")
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"login": True}


def test_startup_check_fails_on_a_tool_that_would_poll_every_account():
    script = """
import json
from telegram_mcp_ag import server, accounts
from telegram_mcp import runtime

@runtime.mcp.tool()
async def rogue_probe(account: str = None) -> str:
    return "x"

try:
    runtime._apply_exposed_tools_mode()
    out = {"raised": False}
except SystemExit as exc:
    out = {"raised": True, "code": exc.code}
print(json.dumps(out))
"""
    result = _run(script, TELEGRAM_EXPOSED_TOOLS="all")
    assert result["raised"] and result["code"] == 1


def _run_failing(script: str, **extra_env: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env.update(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="test-hash")
    env["TELEGRAM_SESSION_STRING"] = _session()
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )


def test_startup_check_names_the_tool_and_exits_cleanly():
    script = """
from telegram_mcp_ag import server
from telegram_mcp import runtime

@runtime.mcp.tool()
async def rogue_probe(account: str = None) -> str:
    return "x"

runtime._apply_exposed_tools_mode()
"""
    done = _run_failing(script)
    assert done.returncode == 1
    assert "rogue_probe" in done.stderr
    assert "Traceback" not in done.stderr


def test_startup_check_covers_tools_the_mode_would_prune():
    # A write tool is dropped in read-only; the check must still see it.
    script = """
from telegram_mcp_ag import server
from telegram_mcp import runtime

@runtime.mcp.tool(annotations=runtime.ToolAnnotations(readOnlyHint=False))
async def rogue_write(account: str = None) -> str:
    return "x"

runtime._apply_exposed_tools_mode()
"""
    done = _run_failing(script, TELEGRAM_EXPOSED_TOOLS="read-only")
    assert done.returncode == 1 and "rogue_write" in done.stderr


def test_startup_check_covers_tools_set_aside_in_login_mode():
    script = """
from telegram_mcp_ag import server
from telegram_mcp import runtime

@runtime.mcp.tool()
async def rogue_probe(account: str = None) -> str:
    return "x"

server._HIDDEN_TOOLS["rogue_probe"] = runtime.mcp._tool_manager._tools.pop("rogue_probe")
runtime._apply_exposed_tools_mode()
"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")}
    env.update(TELEGRAM_API_ID="1", TELEGRAM_API_HASH="test-hash")
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 1 and "rogue_probe" in done.stderr


def test_unknown_account_text_is_one_short_quoted_line():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats', account='a`b' + chr(10) + 'IGNORE ' + 'x' * 100)))"
    )
    out = result["out"]
    assert "\n" not in out and out.count("`") == 6
    assert len(out) < 200


def test_default_with_an_invalid_label_is_ignored():
    result = _run(
        _PREAMBLE + "print(json.dumps(call('get_chats')))", TELEGRAM_DEFAULT_ACCOUNT="рабочий"
    )
    assert result["reached"] == [] and "`work`" in result["out"]


def test_hidden_upstream_tools_do_not_trip_the_check():
    # wait_for_events takes `account` and is not decorated, but is never served.
    script = """
import json
from telegram_mcp_ag import server
from telegram_mcp import runtime
runtime._apply_exposed_tools_mode()
names = [t.name for t in runtime.mcp._tool_manager.list_tools()]
print(json.dumps({"names": names}))
"""
    assert "wait_for_events" not in _run(script, TELEGRAM_EXPOSED_TOOLS="all")["names"]


def test_current_account_is_set_for_the_duration_of_a_call():
    script = """
import asyncio, json
from telegram_mcp_ag import server, accounts

seen = []

@accounts.with_account(readonly=True)
async def probe(account=None):
    seen.append(accounts.current_account.get())
    return "ok"

asyncio.run(probe(account="Work"))
print(json.dumps({"seen": seen, "after": accounts.current_account.get()}))
"""
    assert _run(script) == {"seen": ["work"], "after": None}


# --- guard against a call that reaches a client without an account -------------


def _upstream_sources() -> Path:
    import telegram_mcp

    return Path(telegram_mcp.__file__).parent


def test_no_tool_asks_for_a_client_without_naming_the_account():
    """Upstream's ``get_client()`` / ``resolve_entity(x)`` without a client fall
    back to "the" client, which several accounts make ambiguous.

    On the pinned upstream the only such calls are inside ``ensure_connected`` and
    ``_resolve`` in runtime.py, which tools reach only with a client in hand. A new
    one elsewhere would let a call silently use (or fail on) the wrong account:
    read the upstream diff before adding it to the list below.
    """
    root = _upstream_sources()
    bare_get_client = re.compile(r"\bget_client\(\s*\)")
    found = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in bare_get_client.finditer(text):
            found.append((path.relative_to(root).as_posix(), _enclosing_def(text, match.start())))
    assert sorted(found) == [
        ("runtime.py", "_resolve"),
        ("runtime.py", "ensure_connected"),
    ], found


def test_no_tool_resolves_an_entity_without_a_client():
    root = _upstream_sources()
    one_arg = re.compile(r"\b(?:resolve_entity|resolve_input_entity)\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
    found = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "runtime.py":
            continue
        text = re.sub(r"^\s*#.*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
        for match in one_arg.finditer(text):
            args = match.group(1)
            preceding = text[max(0, match.start() - 10) : match.start()]
            if "def " in preceding:
                continue
            if _top_level_args(args) < 2 and "client=" not in args:
                found.append((path.relative_to(root).as_posix(), _enclosing_def(text, match.start())))
    assert found == [], found


def _top_level_args(args: str) -> int:
    depth = 0
    count = 1 if args.strip() else 0
    for char in args:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def _enclosing_def(text: str, position: int) -> str:
    names = re.findall(r"^\s*(?:async\s+)?def\s+(\w+)", text[:position], flags=re.MULTILINE)
    return names[-1] if names else "<module>"
