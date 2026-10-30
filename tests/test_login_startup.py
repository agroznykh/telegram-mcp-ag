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
