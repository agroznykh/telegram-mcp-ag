"""The terminal command that redoes the Telegram login on this machine.

Shown by both server modes: ``server.py`` when Telegram reports the session
dead, ``login.py`` when Telegram rejects the saved api_id/api_hash. Kept here
so neither has to import the other.
"""

import platform

from telegram_mcp_ag import config as _config

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


def _with_account(account):
    """Canonical, checked label for the command line, or ``None``.

    The label is pasted into a shell command the user will run, so it is checked
    here again against the allowed characters instead of trusting the caller.
    """
    if account is None:
        return None
    return _config.validate_label(account)


def instructions(account=None) -> str:
    """The command for this machine's OS, or both if the OS is unknown.

    With ``account`` the command redoes the login of that account only.
    """
    label = _with_account(account)
    mac_linux = CMD_MAC_LINUX if label is None else f"{CMD_MAC_LINUX} --account {label}"
    windows = CMD_WINDOWS if label is None else f"{CMD_WINDOWS} -Account {label}"
    system = platform.system()
    if system == "Darwin" or system == "Linux":
        return f"Выполните на этой машине в терминале: {mac_linux}"
    if system == "Windows":
        return f"Выполните на этой машине в PowerShell: {windows}"
    return (
        "Не удалось определить ОС этой машины, выберите нужную команду -- "
        f"macOS/Linux (терминал): {mac_linux} -- "
        f"Windows (PowerShell): {windows}"
    )
