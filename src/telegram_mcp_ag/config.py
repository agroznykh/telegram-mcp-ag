"""Load Telegram credentials from the environment or ~/telegram-mcp-ag/config.env.

Must run before ``telegram_mcp.runtime`` is imported: that module reads
``TELEGRAM_API_ID``/``TELEGRAM_API_HASH`` from the environment at import time
and exits with a raw traceback or a bare stderr message if they are missing.
"""

import os
import re
import stat
import sys
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

CONFIG_DIR = Path.home() / "telegram-mcp-ag"
CONFIG_PATH = CONFIG_DIR / "config.env"

REQUIRED_VARS = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")
SESSION_STRING_PREFIX = "TELEGRAM_SESSION_STRING_"
SESSION_NAME_PREFIX = "TELEGRAM_SESSION_NAME_"


class ConfigError(Exception):
    """Raised when Telegram credentials are missing or invalid."""


class MissingSessionError(ConfigError):
    """Raised when api_id/api_hash are present but no session is configured.

    Unlike every other :class:`ConfigError`, this one is recoverable without
    the terminal: the server can start in login-only mode and let the user
    scan a QR code from the chat. See ``login.py``.
    """


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


def _drop_unreadable_session_strings(env: dict) -> None:
    """Remove session strings Telethon cannot parse, warning about each.

    A corrupt or truncated string is worth nothing — the upstream runtime builds
    its clients at import time and would die on ``binascii.Error`` before any of
    our error handling runs. Dropping it here turns that crash into the ordinary
    "no session" path, which offers a fresh QR login.
    """
    from telethon.sessions import StringSession

    for key in [k for k in env if k == "TELEGRAM_SESSION_STRING" or k.startswith(SESSION_STRING_PREFIX)]:
        value = str(env.get(key, "")).strip()
        if not value:
            continue
        try:
            StringSession(value)
        except Exception:
            print(
                f"Warning: {key} is not a readable Telegram session string and was "
                "ignored. Log in again to replace it.",
                file=sys.stderr,
            )
            env.pop(key, None)


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


def _quote(value: str) -> str:
    """Render a value for config.env, quoting only when it could be misread."""
    if value and not any(ch in value for ch in " \t\"'#$\\"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_values(values: dict, config_path: Optional[Path] = None) -> None:
    """Merge ``values`` into ``config.env``, keeping the file owner-only.

    Existing keys are rewritten in place and everything else in the file —
    comments, unrelated settings, ordering — is preserved, because this runs
    against files the installer wrote and users may have edited by hand.
    """
    # Resolved here rather than as a default argument so that CONFIG_PATH stays
    # a single source of truth that tests and callers can redirect.
    config_path = CONFIG_PATH if config_path is None else config_path
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    lines = []
    if config_path.exists():
        lines = config_path.read_text(encoding="utf-8").splitlines()

    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[index] = f"{key}={_quote(str(remaining.pop(key)))}"
    for key, value in remaining.items():
        lines.append(f"{key}={_quote(str(value))}")

    # os.open with 0600 rather than write-then-chmod: the latter leaves the
    # credentials world-readable for the moment in between.
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    if os.name == "posix":
        # A pre-existing file keeps its old mode through os.open.
        os.chmod(config_path, 0o600)


def clear_session(config_path: Optional[Path] = None) -> bool:
    """Drop every session key from ``config.env``. Returns whether anything changed.

    Called when Telegram reports the session as revoked: a dead session left
    in place would make every future launch skip login-only mode and hand the
    user the same opaque failure again. Clearing it here is what lets the next
    process start (an extension toggle, a client restart -- no terminal) land
    on the ordinary "no session" path and offer a fresh QR login in chat.
    """
    config_path = CONFIG_PATH if config_path is None else config_path
    if not config_path.exists():
        return False

    lines = config_path.read_text(encoding="utf-8").splitlines()

    def _is_session_key(line: str) -> bool:
        key = line.split("=", 1)[0].strip()
        return (
            key in ("TELEGRAM_SESSION_STRING", "TELEGRAM_SESSION_NAME")
            or key.startswith(SESSION_STRING_PREFIX)
            or key.startswith(SESSION_NAME_PREFIX)
        )

    kept = [line for line in lines if not _is_session_key(line)]
    if len(kept) == len(lines):
        return False

    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(kept) + ("\n" if kept else ""))
    if os.name == "posix":
        os.chmod(config_path, 0o600)
    return True


DEFAULT_EXPOSED_TOOLS = "read-only"
DOWNLOAD_SUBDIR = "Telegram"
DOWNLOAD_TOOL = "download_media"
_DOWNLOADS_FOLDER_ID = "{374DE290-123F-4565-9164-39C4925E467B}"


def _windows_downloads_dir() -> Optional[Path]:
    """The Downloads known folder, which the user may have moved to another drive."""
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder_id = _GUID()
    ctypes.windll.ole32.CLSIDFromString(_DOWNLOADS_FOLDER_ID, ctypes.byref(folder_id))
    buffer = ctypes.c_wchar_p()
    result = ctypes.windll.shell32.SHGetKnownFolderPath(
        ctypes.byref(folder_id), 0, None, ctypes.byref(buffer)
    )
    try:
        if result != 0 or not buffer.value:
            return None
        return Path(buffer.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(buffer)


def _linux_downloads_dir() -> Optional[Path]:
    """``XDG_DOWNLOAD_DIR`` from ``~/.config/user-dirs.dirs`` (``Загрузки`` in Russian)."""
    home = Path.home()
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_dir = Path(config_home) if config_home and os.path.isabs(config_home) else home / ".config"
    try:
        text = (config_dir / "user-dirs.dirs").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("XDG_DOWNLOAD_DIR="):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        if not value:
            return None
        path = Path(value.replace("${HOME}", str(home)).replace("$HOME", str(home)))
        # A relative path would be resolved against whatever folder the client
        # started the server in.
        return path if path.is_absolute() else None
    return None


def default_download_root() -> Path:
    """``<Downloads>/Telegram``: where files go when the client names no folder.

    Does not create the folder. Any failure to locate the system Downloads
    folder falls back to ``~/Downloads``.
    """
    downloads: Optional[Path] = None
    try:
        if sys.platform == "win32":
            downloads = _windows_downloads_dir()
        elif sys.platform.startswith("linux"):
            downloads = _linux_downloads_dir()
    except Exception:
        downloads = None
    if downloads is None:
        downloads = Path.home() / "Downloads"
    return downloads / DOWNLOAD_SUBDIR


def download_defaults() -> dict:
    """The folder settings that enable ``download_media`` without client folders."""
    values = {
        "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK": "1",
        "TELEGRAM_DOWNLOAD_SUBDIR": DOWNLOAD_SUBDIR,
    }
    root = str(default_download_root())
    # Upstream splits this list on ; , and : (a drive colon excepted). A path
    # containing one would turn into a wider parent folder plus a relative one,
    # so such a path is not offered as a fallback at all.
    if not re.search(r"[;,]|(?<!^[A-Za-z]):", root):
        values["TELEGRAM_ALLOWED_ROOTS"] = root
    return values


def _ensure_exposed_tools_line(config_path: Path) -> None:
    """Add the default exposure mode to a config that lacks it.

    Creates the file if there is none: the login flow and hand-written files
    don't include this line either. Having it in the file means changing the mode
    is editing a value, not looking up a key. A value already there, even a
    different one, is the user's decision. A file that can't be written is no
    problem: server.py applies the same default anyway.
    """
    if config_path.exists() and "TELEGRAM_EXPOSED_TOOLS" in dotenv_values(config_path):
        return
    try:
        write_values({"TELEGRAM_EXPOSED_TOOLS": DEFAULT_EXPOSED_TOOLS}, config_path)
    except OSError:
        pass


def download_media_enabled(mode: Optional[str]) -> bool:
    """Whether a ``TELEGRAM_EXPOSED_TOOLS`` value leaves ``download_media`` registered.

    Mirrors upstream's grammar (``all`` or ``read-only``, then ``+tool,tool`` for
    ``read-only`` only, then ``-tool,tool``), so that the folders are set up
    exactly when the tool exists. tests/test_server.py checks it against upstream.
    An invalid value is "no": upstream rejects it at startup anyway.
    """
    value = (mode or "").strip().lower()
    for base in ("read-only", "all"):
        rest = value[len(base) :]
        if value.startswith(base) and rest[:1] in ("", "+", "-"):
            break
    else:
        return False

    def names(text: str) -> list:
        return [part.strip() for part in text.split(",") if part.strip()]

    allowlist, exclusions = [], []
    if rest.startswith("+"):
        head, separator, tail = rest[1:].partition("-")
        allowlist = names(head)
        exclusions = names(tail) if separator else []
    elif rest.startswith("-"):
        exclusions = names(rest[1:])
    if DOWNLOAD_TOOL in exclusions:
        return False
    return base == "all" or DOWNLOAD_TOOL in allowlist


def _effective_exposed_tools(file_values: dict) -> str:
    """The mode the server will run with: environment, then file, then default."""
    for source in (os.environ.get("TELEGRAM_EXPOSED_TOOLS"), file_values.get("TELEGRAM_EXPOSED_TOOLS")):
        if source and source.strip():
            return source
    return DEFAULT_EXPOSED_TOOLS


def _ensure_download_lines(config_path: Path) -> None:
    """Add the download-folder keys that a config lacks, if downloads are enabled.

    With ``download_media`` not exposed the folders would only be a stray folder
    and a system access prompt, so nothing is written. Each key is judged on its
    own: one already in the file, even empty or with another value, is the
    user's decision and stays. A file that can't be written is no problem:
    server.py fills the same defaults into the environment.
    """
    present = dotenv_values(config_path) if config_path.exists() else {}
    if not download_media_enabled(_effective_exposed_tools(present)):
        return
    missing = {k: v for k, v in download_defaults().items() if k not in present}
    if not missing:
        return
    try:
        write_values(missing, config_path)
    except OSError:
        pass


def load(config_path: Path = CONFIG_PATH) -> None:
    """Populate ``os.environ`` with Telegram credentials.

    Priority: variables already present in the environment win; missing ones
    are filled in from ``config_path`` if it exists.

    Raises:
        ConfigError: required credentials are still missing or invalid.
    """
    # In this order: writing the line also fixes the file's permissions, and the
    # warning about them has to be seen first.
    if config_path.exists():
        _warn_if_not_locked_down(config_path)
    _ensure_exposed_tools_line(config_path)
    _ensure_download_lines(config_path)
    if config_path.exists():
        for key, value in dotenv_values(config_path).items():
            if value is None:
                continue
            # Not setdefault: a client can set an env var to an empty string
            # rather than omitting it. Treating "" as a real value would let
            # that shadow config.env and make working credentials look missing.
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

    # Validated before the session check: MissingSessionError is recoverable and
    # lets the server start in login-only mode, where telegram_mcp.runtime still
    # does int(TELEGRAM_API_ID) at import and would die on a raw ValueError.
    try:
        int(os.environ["TELEGRAM_API_ID"].strip())
    except ValueError:
        raise ConfigError(
            f"TELEGRAM_API_ID must be a number, got {os.environ['TELEGRAM_API_ID']!r}."
        ) from None

    _drop_unreadable_session_strings(os.environ)

    if not _has_session(os.environ):
        raise MissingSessionError(
            "No Telegram session configured. Set TELEGRAM_SESSION_STRING in "
            f"{config_path} (or the environment), or run the installer to log in."
        )
