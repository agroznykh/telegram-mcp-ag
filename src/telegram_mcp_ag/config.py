"""Load Telegram credentials from the environment or ~/telegram-mcp-ag/config.env.

Must run before ``telegram_mcp.runtime`` is imported: that module reads
``TELEGRAM_API_ID``/``TELEGRAM_API_HASH`` from the environment at import time
and exits with a raw traceback or a bare stderr message if they are missing.
"""

import os
import stat
import sys
from pathlib import Path

from dotenv import dotenv_values

CONFIG_DIR = Path.home() / "telegram-mcp-ag"
CONFIG_PATH = CONFIG_DIR / "config.env"

REQUIRED_VARS = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")
SESSION_STRING_PREFIX = "TELEGRAM_SESSION_STRING_"
SESSION_NAME_PREFIX = "TELEGRAM_SESSION_NAME_"


class ConfigError(Exception):
    """Raised when Telegram credentials are missing or invalid."""


def _warn_if_not_locked_down(path: Path) -> None:
    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: {path} is readable by other users (mode {oct(mode)}). "
            f"Run `chmod 600 {path}` to protect your Telegram credentials.",
            file=sys.stderr,
        )


def _has_session(env: dict) -> bool:
    # A key that exists but holds a blank value is not a session: see the note
    # about empty form fields in load().
    return any(
        (
            key in ("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME")
            or key.startswith(SESSION_STRING_PREFIX)
            or key.startswith(SESSION_NAME_PREFIX)
        )
        and str(value).strip()
        for key, value in env.items()
    )


def load(config_path: Path = CONFIG_PATH) -> None:
    """Populate ``os.environ`` with Telegram credentials.

    Priority: variables already present in the environment win; missing ones
    are filled in from ``config_path`` if it exists.

    Raises:
        ConfigError: required credentials are still missing or invalid.
    """
    if config_path.exists():
        _warn_if_not_locked_down(config_path)
        for key, value in dotenv_values(config_path).items():
            if value is None:
                continue
            # Not setdefault: the .mcpb bundle runs under Claude Desktop, which
            # substitutes an empty string for any form field the user left
            # blank. Treating "" as a real value would let a blank form shadow
            # config.env and make working credentials look missing.
            if not os.environ.get(key, "").strip():
                os.environ[key] = value

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name, "").strip()]
    if missing:
        raise ConfigError(
            "Missing Telegram credentials: "
            + ", ".join(missing)
            + f". Run the installer to set up {config_path}, or set these "
            "environment variables yourself."
        )

    if not _has_session(os.environ):
        raise ConfigError(
            "No Telegram session configured. Set TELEGRAM_SESSION_STRING in "
            f"{config_path} (or the environment), or run the installer to log in."
        )

    try:
        int(os.environ["TELEGRAM_API_ID"].strip())
    except ValueError:
        raise ConfigError(
            f"TELEGRAM_API_ID must be a number, got {os.environ['TELEGRAM_API_ID']!r}."
        ) from None
