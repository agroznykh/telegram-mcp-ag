"""Starts the server in login mode on the interpreter running the tests.

Guards the event loop setup in ``server.main``: nest_asyncio used to patch it
there, which made the first ``asyncio.wait_for`` inside Telethon's connect()
raise "Timeout should be used inside a task" on Python 3.14+. The unit tests
for the login tools never reach that code, so they stayed green while login
was broken. Runs in a fresh interpreter because importing the server registers
tools on a process-wide MCP instance.
"""

import os
import subprocess
import sys

_PROBE = """
import asyncio
import sys

from telegram_mcp_ag import server

assert server._LOGIN_MODE


async def connect():
    await asyncio.wait_for(asyncio.sleep(0), timeout=1)


async def disconnect():
    return None


async def serve():
    await asyncio.wait_for(asyncio.sleep(0), timeout=1)
    print("login-server-ok")


for client in server._runtime.clients.values():
    client.connect = connect
    client.disconnect = disconnect
server.mcp.run_stdio_async = serve
sys.argv = ["telegram-mcp-ag"]
server.main()
"""


_DEAD_SESSION_PROBE = """
import telethon

attempts = []


async def connect(self, *args, **kwargs):
    attempts.append("connect")
    raise AssertionError("the server must not touch Telegram while starting")


telethon.TelegramClient.connect = connect

from telegram_mcp_ag import server

assert not server._LOGIN_MODE
assert not attempts
print("revoked-session-starts-normally")
"""


def test_saved_session_is_not_checked_at_startup_and_stays_in_the_config(tmp_path):
    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=os.urandom(256))

    config_dir = tmp_path / "telegram-mcp-ag"
    config_dir.mkdir()
    config_path = config_dir / "config.env"
    written = (
        "TELEGRAM_API_ID=1\n"
        "TELEGRAM_API_HASH=test-hash\n"
        f"TELEGRAM_SESSION_STRING={session.save()}\n"
    )
    config_path.write_text(written, encoding="utf-8")
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")},
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
    }
    done = subprocess.run(
        [sys.executable, "-c", _DEAD_SESSION_PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert "revoked-session-starts-normally" in done.stdout
    # A dead session is found on its first use (connections.py), not here: the
    # file keeps it, whatever the network says at startup.
    remaining = config_path.read_text(encoding="utf-8")
    assert "TELEGRAM_SESSION_STRING=" in remaining


def test_login_mode_starts_and_serves(tmp_path):
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("TELEGRAM_")},
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "test-hash",
    }
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert "login-server-ok" in done.stdout
