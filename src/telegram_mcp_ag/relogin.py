"""The terminal command that redoes the Telegram login on this machine.

Shown by both server modes: ``server.py`` when Telegram reports the session
dead, ``login.py`` when Telegram rejects the saved api_id/api_hash. Kept here
so neither has to import the other.
"""

import platform

# How to make a client start this server afresh. The installers register it
# as a local MCP server (claude_desktop_config.json), not as a Claude Desktop
# extension, so there is no per-server toggle there: the app itself restarts.
RESTART_CLIENT = (
    "перезапустите MCP-клиент и начните новый разговор: Claude Desktop и ChatGPT "
    "Desktop -- выйдите из приложения полностью, а не просто закройте окно (на "
    "Mac -- Cmd+Q), и откройте снова; Claude Code -- команда /mcp, "
    "переподключить сервер telegram-mcp-ag, или новая сессия; Codex CLI -- "
    "новая сессия"
)

CMD_MAC_LINUX = (
    "curl -fsSL https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.sh"
    " | bash -s -- --relogin"
)
CMD_WINDOWS = (
    '$f = "$env:TEMP\\telegram-mcp-ag-install.ps1"; '
    "iwr 'https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.ps1'"
    " -OutFile $f; powershell -NoProfile -ExecutionPolicy Bypass -File $f -Relogin"
)


def instructions() -> str:
    """The command for this machine's OS, or both if the OS is unknown."""
    system = platform.system()
    if system == "Darwin" or system == "Linux":
        return f"Выполните на этой машине в терминале: {CMD_MAC_LINUX}"
    if system == "Windows":
        return f"Выполните на этой машине в PowerShell: {CMD_WINDOWS}"
    return (
        "Не удалось определить ОС этой машины, выберите нужную команду -- "
        f"macOS/Linux (терминал): {CMD_MAC_LINUX} -- "
        f"Windows (PowerShell): {CMD_WINDOWS}"
    )
