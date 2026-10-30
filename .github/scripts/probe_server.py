"""Starts an installed server over stdio and checks it serves the login tools."""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = {"telegram_login_start", "telegram_login_check"}


async def main(command: str) -> None:
    home = str(Path.home())
    params = StdioServerParameters(
        command=command,
        env={
            "HOME": home,
            "USERPROFILE": home,
            "TELEGRAM_API_ID": "1",
            "TELEGRAM_API_HASH": "dummy",
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
    assert tools == EXPECTED, tools
    print("server is up, tools:", sorted(tools))


asyncio.run(asyncio.wait_for(main(sys.argv[1]), 120))
