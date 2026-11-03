"""The terminal command that redoes the Telegram login on this machine.

Shown by both server modes: ``server.py`` when Telegram reports the session
dead, ``login.py`` when Telegram rejects the saved api_id/api_hash. Kept here
so neither has to import the other.
"""

import platform

CMD_MAC_LINUX = (
    "curl -fsSL https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.sh"
    " | bash -s -- --relogin"
)
CMD_WINDOWS = (
    '$f = "$env:TEMP\\telegram-mcp-ag-install.ps1"; '
    "iwr 'https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.ps1'"
    " -OutFile $f; & $f -Relogin"
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
