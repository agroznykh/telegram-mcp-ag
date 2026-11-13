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


DEFAULT_LABEL = "default"
MAX_LABEL_LENGTH = 32
DEFAULT_ACCOUNT_KEY = "TELEGRAM_DEFAULT_ACCOUNT"
_UNNAMED_STRING_KEY = "TELEGRAM_SESSION_STRING"
_UNNAMED_NAME_KEY = "TELEGRAM_SESSION_NAME"
# ASCII only: str.isalnum() would let Cyrillic or other scripts through, and the
# label ends up in an environment variable name and in a shell command.
_LABEL_RE = re.compile(r"[A-Za-z0-9_-]{1,%d}" % MAX_LABEL_LENGTH, re.ASCII)


def normalize_label(label: str) -> str:
    """Fold a label to its canonical form: lowercase, ``-`` as ``_``.

    The same rule as ``telegram_mcp.account_labels.canonical_account_label``
    (tests/test_config.py compares them): the upstream runtime folds the suffix
    of a key the same way, so every spelling of one label has to meet here too.
    Does not check that the label is allowed -- see :func:`validate_label`.
    """
    return label.strip().lower().replace("-", "_")


def validate_label(label: str, *, allow_default: bool = True) -> str:
    """Return the canonical form of ``label`` or raise :class:`ConfigError`.

    An allowed label is Latin letters, digits, ``_`` and ``-``, up to 32
    characters. The strictness is about safety, not taste: the label becomes part
    of a ``config.env`` key and of the relogin command shown to the user. The
    message is shown as it is, so it says what to do instead.
    """
    text = label.strip()
    if not _LABEL_RE.fullmatch(text):
        raise ConfigError(
            f"Метка аккаунта {text!r} не подходит: только латинские буквы, цифры, "
            f"«_» и «-», не длиннее {MAX_LABEL_LENGTH} символов. Назовите латиницей, "
            "например «рабочий» -> work, «личный» -> personal."
        )
    canonical = normalize_label(text)
    if canonical == DEFAULT_LABEL and not allow_default:
        raise ConfigError(
            f"Метка {DEFAULT_LABEL!r} зарезервирована за аккаунтом без названия. "
            "Выберите другую, например personal или work."
        )
    return canonical


def session_key(label: str) -> str:
    """The ``config.env`` key that holds the session string of ``label``."""
    canonical = validate_label(label)
    if canonical == DEFAULT_LABEL:
        return _UNNAMED_STRING_KEY
    return SESSION_STRING_PREFIX + canonical.upper()


def _key_kind_and_suffix(key: str):
    """Split a session key into ``("string" | "name", suffix)``, or ``None``.

    The suffix is ``None`` for the unnamed keys and may be empty or malformed for
    a hand-written one: the caller decides what that means.
    """
    key = key.strip()
    if key == _UNNAMED_STRING_KEY:
        return "string", None
    if key == _UNNAMED_NAME_KEY:
        return "name", None
    if key.startswith(SESSION_STRING_PREFIX):
        return "string", key[len(SESSION_STRING_PREFIX) :]
    if key.startswith(SESSION_NAME_PREFIX):
        return "name", key[len(SESSION_NAME_PREFIX) :]
    return None


def _label_of_key(key: str):
    """Canonical label a session key belongs to; ``None`` if it is not a session
    key or its label is not allowed."""
    parsed = _key_kind_and_suffix(key)
    if parsed is None:
        return None
    suffix = parsed[1]
    if suffix is None:
        return DEFAULT_LABEL
    try:
        return validate_label(suffix)
    except ConfigError:
        return None


def configured_labels(env) -> list:
    """Labels of the accounts that have a session in ``env``, ``default`` first.

    Counts both ``..._STRING`` and ``..._NAME`` keys, skips blank values and
    labels that are not allowed. For the CLI and the installers: the server
    counts accounts by what the upstream runtime built (``runtime.clients``).
    """
    labels = {
        label
        for key, value in env.items()
        if value is not None and str(value).strip() and (label := _label_of_key(key)) is not None
    }
    return sorted(labels, key=lambda label: (label != DEFAULT_LABEL, label))


def is_multi(env) -> bool:
    return len(configured_labels(env)) >= 2


def default_account(env):
    """The label ``TELEGRAM_DEFAULT_ACCOUNT`` names, or ``None``.

    Blank means "not set" like every other setting. A label with no session
    behind it is ignored with a warning: it would otherwise send every call
    without ``account`` to an account that does not exist.
    """
    value = str(env.get(DEFAULT_ACCOUNT_KEY) or "").strip()
    if not value:
        return None
    try:
        label = validate_label(value)
    except ConfigError:
        label = None
    if label is None or label not in configured_labels(env):
        print(
            f"Warning: {DEFAULT_ACCOUNT_KEY} names an account that is not configured "
            "and was ignored.",
            file=sys.stderr,
        )
        return None
    return label


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


def _drop_unusable_session_keys(env: dict) -> None:
    """Remove session keys the upstream runtime must not see, warning about each.

    Two kinds. A key whose label is not allowed (for instance typed in by hand):
    the label would end up in the relogin command, so it is never an account here.
    And ``TELEGRAM_SESSION_STRING_DEFAULT`` next to the unnamed key: both mean the
    same account and the upstream would silently pick one of them.
    """
    unnamed_in_use = bool(str(env.get(_UNNAMED_STRING_KEY, "")).strip())
    for key in list(env):
        parsed = _key_kind_and_suffix(key)
        if parsed is None or parsed[1] is None:
            continue
        label = _label_of_key(key)
        if label is None:
            reason = "its account label is not allowed"
        elif label == DEFAULT_LABEL and parsed[0] == "string" and unnamed_in_use:
            reason = f"it duplicates {_UNNAMED_STRING_KEY}"
        else:
            continue
        if str(env.get(key, "")).strip():
            print(f"Warning: {key!r} was ignored: {reason}.", file=sys.stderr)
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


def _read_lines(config_path: Path) -> list:
    if not config_path.exists():
        return []
    return config_path.read_text(encoding="utf-8").splitlines()


def _write_lines(config_path: Path, lines: list) -> None:
    """Rewrite ``config.env`` in place, owner-only.

    In place (``O_TRUNC``) and not via a temporary file plus ``os.replace``: on
    Windows a new file would lose the ACL the installer gave this one.
    """
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # os.open with 0600 rather than write-then-chmod: the latter leaves the
    # credentials world-readable for the moment in between.
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + ("\n" if lines else ""))
    if os.name == "posix":
        # A pre-existing file keeps its old mode through os.open.
        os.chmod(config_path, 0o600)


def _line_key(line: str) -> str:
    return line.split("=", 1)[0].strip()


def write_values(values: dict, config_path: Optional[Path] = None) -> None:
    """Merge ``values`` into ``config.env``, keeping the file owner-only.

    Existing keys are rewritten in place and everything else in the file —
    comments, unrelated settings, ordering — is preserved, because this runs
    against files the installer wrote and users may have edited by hand.
    """
    # Resolved here rather than as a default argument so that CONFIG_PATH stays
    # a single source of truth that tests and callers can redirect.
    config_path = CONFIG_PATH if config_path is None else config_path
    lines = _read_lines(config_path)

    remaining = dict(values)
    for index, line in enumerate(lines):
        key = _line_key(line)
        if key in remaining:
            lines[index] = f"{key}={_quote(str(remaining.pop(key)))}"
    for key, value in remaining.items():
        lines.append(f"{key}={_quote(str(value))}")

    _write_lines(config_path, lines)


def _is_session_line(line: str, label: Optional[str]) -> bool:
    """Whether ``line`` holds a session key -- of ``label`` only, if one is given.

    Compared by canonical label, so ``TELEGRAM_SESSION_STRING_Work`` typed in by
    hand counts as the ``work`` account like the key we write.
    """
    if label is None:
        return _key_kind_and_suffix(_line_key(line)) is not None
    return _label_of_key(_line_key(line)) == label


def write_session(label: str, value: str, config_path: Optional[Path] = None) -> None:
    """Save the session string of ``label``, leaving every other account alone.

    A key of the same account spelled differently (a hand-written lowercase
    suffix) is dropped, so a relogin replaces the session instead of leaving
    a second, dead one beside it for the upstream to pick.
    """
    canonical = validate_label(label)
    key = session_key(canonical)
    config_path = CONFIG_PATH if config_path is None else config_path

    lines = []
    placed = False
    for line in _read_lines(config_path):
        line_key = _line_key(line)
        parsed = _key_kind_and_suffix(line_key)
        if parsed is not None and parsed[0] == "string" and _label_of_key(line_key) == canonical:
            if line_key == key and not placed:
                lines.append(f"{key}={_quote(value)}")
                placed = True
            continue
        lines.append(line)
    if not placed:
        lines.append(f"{key}={_quote(value)}")
    _write_lines(config_path, lines)


def clear_session(config_path: Optional[Path] = None, label: Optional[str] = None) -> bool:
    """Drop session keys from ``config.env``. Returns whether anything changed.

    With ``label`` only that account's keys go; without, every session key (the
    single-account mode, where the one dead session is the whole picture).

    Called when Telegram reports the session as revoked: a dead session left
    in place would make every future launch skip login-only mode and hand the
    user the same opaque failure again. Clearing it here is what lets the next
    process start (an extension toggle, a client restart -- no terminal) land
    on the ordinary "no session" path and offer a fresh QR login in chat.
    """
    config_path = CONFIG_PATH if config_path is None else config_path
    if not config_path.exists():
        return False
    canonical = None if label is None else validate_label(label)

    lines = _read_lines(config_path)
    kept = [line for line in lines if not _is_session_line(line, canonical)]
    if len(kept) == len(lines):
        return False

    _write_lines(config_path, kept)
    return True


def rename_session(old: str, new: str, config_path: Optional[Path] = None) -> None:
    """Move the session of ``old`` under the label ``new``, in one write.

    ``TELEGRAM_DEFAULT_ACCOUNT`` follows if it named ``old``. Refuses, changing
    nothing, when ``old`` has no session or ``new`` already has one: silently
    overwriting a live session would lose an account.
    """
    old_label = validate_label(old)
    new_label = validate_label(new, allow_default=False)
    config_path = CONFIG_PATH if config_path is None else config_path

    values = dotenv_values(config_path) if config_path.exists() else {}
    value = next(
        (
            str(v).strip()
            for k, v in values.items()
            if v is not None
            and _key_kind_and_suffix(k) is not None
            and _key_kind_and_suffix(k)[0] == "string"
            and _label_of_key(k) == old_label
            and str(v).strip()
        ),
        "",
    )
    if not value:
        raise ConfigError(f"У аккаунта {old_label!r} нет сохранённой сессии, переименовывать нечего.")
    if new_label in configured_labels(values):
        raise ConfigError(f"Аккаунт с меткой {new_label!r} уже есть.")

    new_key = session_key(new_label)
    default_was_old = normalize_label(str(values.get(DEFAULT_ACCOUNT_KEY) or "")) == old_label
    lines = []
    placed = False
    for line in _read_lines(config_path):
        line_key = _line_key(line)
        label = _label_of_key(line_key)
        if label == old_label or label == new_label:
            # Both ends of the move: the old keys go, blank leftovers of the new
            # label go too, and the value lands where the first old key stood.
            if not placed and label == old_label:
                lines.append(f"{new_key}={_quote(value)}")
                placed = True
            continue
        if line_key == DEFAULT_ACCOUNT_KEY and default_was_old:
            line = f"{DEFAULT_ACCOUNT_KEY}={new_label}"
        lines.append(line)
    if not placed:
        lines.append(f"{new_key}={_quote(value)}")
    _write_lines(config_path, lines)


def set_default_account(label: Optional[str], config_path: Optional[Path] = None) -> None:
    """Set ``TELEGRAM_DEFAULT_ACCOUNT``, or remove it when ``label`` is ``None``.

    Refuses a label that has no session in the file: such a default would be
    ignored with a warning at every start.
    """
    config_path = CONFIG_PATH if config_path is None else config_path
    if label is None:
        lines = _read_lines(config_path)
        kept = [line for line in lines if _line_key(line) != DEFAULT_ACCOUNT_KEY]
        if len(kept) != len(lines):
            _write_lines(config_path, kept)
        return
    canonical = validate_label(label)
    values = dotenv_values(config_path) if config_path.exists() else {}
    if canonical not in configured_labels(values):
        known = ", ".join(configured_labels(values)) or "ни одного"
        raise ConfigError(f"Аккаунта {canonical!r} нет в настройках. Сейчас есть: {known}.")
    write_values({DEFAULT_ACCOUNT_KEY: canonical}, config_path)


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


def _ensure_default_lines(config_path: Path, present: dict) -> None:
    """Add the settings that ``config_path`` lacks, in a single write.

    ``present`` is what the file holds now. Always the exposure mode; the
    download-folder keys only when ``download_media`` is exposed, since otherwise
    they would only create a stray folder and a system access prompt. Creates
    the file if there is none: the login flow and hand-written files don't
    include these lines either. Each key is judged on its own: one already in the
    file, even empty or with another value, is the user's decision and stays.
    A file that can't be written is no problem: server.py applies the same
    defaults to the environment.
    """
    wanted = {}
    if "TELEGRAM_EXPOSED_TOOLS" not in present:
        wanted["TELEGRAM_EXPOSED_TOOLS"] = DEFAULT_EXPOSED_TOOLS
    if download_media_enabled(_effective_exposed_tools({**present, **wanted})):
        wanted.update({k: v for k, v in download_defaults().items() if k not in present})
    if not wanted:
        return
    try:
        write_values(wanted, config_path)
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
    _ensure_default_lines(config_path, dotenv_values(config_path) if config_path.exists() else {})
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

    _drop_unusable_session_keys(os.environ)
    _drop_unreadable_session_strings(os.environ)

    if not _has_session(os.environ):
        raise MissingSessionError(
            "No Telegram session configured. Set TELEGRAM_SESSION_STRING in "
            f"{config_path} (or the environment), or run the installer to log in."
        )
