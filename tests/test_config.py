import os
import stat
import sys

import pytest

from telethon.crypto import AuthKey

from telegram_mcp_ag import config

TELEGRAM_ENV_PREFIX = "TELEGRAM_"


@pytest.fixture(autouse=True)
def clean_telegram_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(TELEGRAM_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def _valid_session_string(dc_id=2):
    """A structurally valid, unauthorized session string (all-zero auth key).

    dc_id varies it, for tests that need two distinguishable sessions.
    """
    from telethon.sessions import StringSession

    session = StringSession()
    session.set_dc(dc_id, "149.154.167.51", 443)
    session.auth_key = AuthKey(b"\x00" * 256)
    return session.save()


def _write_config(path, **values):
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return path


def test_loads_missing_vars_from_file(tmp_path):
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="123",
        TELEGRAM_API_HASH="hash-from-file",
        TELEGRAM_SESSION_STRING=_valid_session_string(),
    )

    config.load(config_path)

    assert os.environ["TELEGRAM_API_ID"] == "123"
    assert os.environ["TELEGRAM_API_HASH"] == "hash-from-file"
    assert os.environ["TELEGRAM_SESSION_STRING"] == _valid_session_string()


def test_env_vars_take_priority_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash-from-env")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", _valid_session_string(dc_id=4))
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="999",
        TELEGRAM_API_HASH="hash-from-file",
        TELEGRAM_SESSION_STRING=_valid_session_string(),
    )

    config.load(config_path)

    assert os.environ["TELEGRAM_API_HASH"] == "hash-from-env"
    assert os.environ["TELEGRAM_SESSION_STRING"] == _valid_session_string(dc_id=4)


def test_blank_env_vars_fall_back_to_file(tmp_path, monkeypatch):
    # A client can set an env var to an empty string rather than omitting it
    # (e.g. a config template with an unfilled placeholder) -- that must not
    # shadow a working config.env.
    monkeypatch.setenv("TELEGRAM_API_ID", "")
    monkeypatch.setenv("TELEGRAM_API_HASH", "   ")
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="123",
        TELEGRAM_API_HASH="hash-from-file",
        TELEGRAM_SESSION_STRING=_valid_session_string(),
    )

    config.load(config_path)

    assert os.environ["TELEGRAM_API_ID"] == "123"
    assert os.environ["TELEGRAM_API_HASH"] == "hash-from-file"


def test_blank_session_var_does_not_count_as_a_session(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "")

    with pytest.raises(config.ConfigError, match="No Telegram session"):
        config.load(tmp_path / "missing.env")


def test_missing_file_is_not_an_error_when_env_is_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", _valid_session_string())

    config.load(tmp_path / "does-not-exist.env")


def test_missing_required_vars_raises_with_helpful_message(tmp_path):
    with pytest.raises(config.ConfigError) as excinfo:
        config.load(tmp_path / "does-not-exist.env")

    message = str(excinfo.value)
    assert "TELEGRAM_API_ID" in message
    assert "TELEGRAM_API_HASH" in message
    assert "installer" in message


def test_missing_session_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")

    # A distinct subclass, because this is the one failure the server can
    # recover from on its own: it starts in login-only mode instead of exiting.
    with pytest.raises(config.MissingSessionError, match="session"):
        config.load(tmp_path / "does-not-exist.env")


def test_bad_api_id_beats_a_missing_session(monkeypatch, tmp_path):
    # Order matters: login-only mode still lets telegram_mcp.runtime run
    # int(TELEGRAM_API_ID) at import, so a junk value has to be fatal here
    # rather than surfacing later as a raw ValueError.
    monkeypatch.setenv("TELEGRAM_API_ID", "not-a-number")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")

    with pytest.raises(config.ConfigError, match="TELEGRAM_API_ID"):
        config.load(tmp_path / "does-not-exist.env")


def test_unreadable_session_string_becomes_a_login_prompt(monkeypatch, tmp_path, capsys):
    # Telethon builds its clients at import time and would crash on a truncated
    # string with binascii.Error. Treating it as "no session" instead sends the
    # user to the QR login, which replaces it.
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "1BVtsOKgBu5s")

    with pytest.raises(config.MissingSessionError):
        config.load(tmp_path / "does-not-exist.env")

    assert "not a readable Telegram session string" in capsys.readouterr().err
    assert "TELEGRAM_SESSION_STRING" not in os.environ


def test_readable_session_string_is_kept(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", _valid_session_string())

    config.load(tmp_path / "does-not-exist.env")

    assert os.environ["TELEGRAM_SESSION_STRING"]


def test_load_adds_a_missing_exposed_tools_line(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", _valid_session_string())
    config_path = tmp_path / "config.env"
    config_path.write_text("# my note\nOTHER=1\n", encoding="utf-8")

    config.load(config_path=config_path)

    content = config_path.read_text(encoding="utf-8")
    assert content.startswith("# my note\nOTHER=1\nTELEGRAM_EXPOSED_TOOLS=read-only+download_media\n")


@pytest.mark.parametrize(
    "line", ["TELEGRAM_EXPOSED_TOOLS=all", "TELEGRAM_EXPOSED_TOOLS=", "TELEGRAM_EXPOSED_TOOLS=read-only"]
)
def test_load_keeps_an_existing_exposed_tools_line(tmp_path, monkeypatch, line):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", _valid_session_string())
    config_path = tmp_path / "config.env"
    config_path.write_text(f"{line}\n", encoding="utf-8")

    config.load(config_path=config_path)

    assert config_path.read_text(encoding="utf-8").startswith(f"{line}\n")


def test_load_creates_a_missing_config_with_the_exposed_tools_line(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", _valid_session_string())
    config_path = tmp_path / "config.env"

    config.load(config_path=config_path)

    content = config_path.read_text(encoding="utf-8")
    assert content.startswith("TELEGRAM_EXPOSED_TOOLS=read-only+download_media\n")
    assert "TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1\n" in content
    assert "TELEGRAM_DOWNLOAD_SUBDIR=Telegram\n" in content


# ---------------------------------------------------------------------------
# Download folders
# ---------------------------------------------------------------------------


def test_default_download_root_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))

    assert config.default_download_root() == tmp_path / "Downloads" / "Telegram"


def test_default_download_root_on_linux_reads_xdg_user_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "user-dirs.dirs").write_text(
        '# comment\nXDG_DESKTOP_DIR="$HOME/Рабочий стол"\nXDG_DOWNLOAD_DIR="$HOME/Загрузки"\n',
        encoding="utf-8",
    )

    assert config.default_download_root() == tmp_path / "Загрузки" / "Telegram"


@pytest.mark.parametrize("content", [None, "XDG_DESKTOP_DIR=\"$HOME/Desktop\"\n", 'XDG_DOWNLOAD_DIR=""\n'])
def test_default_download_root_on_linux_falls_back_to_downloads(monkeypatch, tmp_path, content):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    if content is not None:
        (tmp_path / ".config").mkdir()
        (tmp_path / ".config" / "user-dirs.dirs").write_text(content, encoding="utf-8")

    assert config.default_download_root() == tmp_path / "Downloads" / "Telegram"


def test_default_download_root_on_windows_uses_the_known_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config, "_windows_downloads_dir", lambda: tmp_path / "D-drive" / "Downloads")

    assert config.default_download_root() == tmp_path / "D-drive" / "Downloads" / "Telegram"


@pytest.mark.parametrize("failure", [None, OSError("boom")])
def test_default_download_root_on_windows_falls_back_when_the_lookup_fails(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))

    def lookup():
        if failure:
            raise failure
        return None

    monkeypatch.setattr(config, "_windows_downloads_dir", lookup)

    assert config.default_download_root() == tmp_path / "Downloads" / "Telegram"


def test_default_download_root_on_linux_honours_xdg_config_home_and_braced_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="${HOME}/Загрузки"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))

    assert config.default_download_root() == tmp_path / "Загрузки" / "Telegram"


def test_default_download_root_on_linux_ignores_a_relative_path(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "user-dirs.dirs").write_text('XDG_DOWNLOAD_DIR="Загрузки"\n', encoding="utf-8")

    assert config.default_download_root() == tmp_path / "Downloads" / "Telegram"


@pytest.mark.parametrize("name", ["Загрузки, архив", "a;b", "a:b"])
def test_download_defaults_skip_a_fallback_path_the_upstream_would_split(monkeypatch, tmp_path, name):
    monkeypatch.setattr(config, "default_download_root", lambda: tmp_path / name / "Telegram")

    values = config.download_defaults()

    assert "TELEGRAM_ALLOWED_ROOTS" not in values
    assert values["TELEGRAM_DOWNLOAD_SUBDIR"] == "Telegram"


def test_ensure_download_lines_adds_the_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_download_root", lambda: tmp_path / "dl" / "Telegram")
    config_path = tmp_path / "config.env"
    config_path.write_text("OTHER=1\n", encoding="utf-8")

    config._ensure_download_lines(config_path)

    values = config.dotenv_values(config_path)
    assert values["OTHER"] == "1"
    assert values["TELEGRAM_ALLOWED_ROOTS"] == str(tmp_path / "dl" / "Telegram")
    assert values["TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK"] == "1"
    assert values["TELEGRAM_DOWNLOAD_SUBDIR"] == "Telegram"


def test_ensure_download_lines_keeps_existing_keys_even_when_empty(tmp_path):
    config_path = tmp_path / "config.env"
    config_path.write_text(
        "TELEGRAM_ALLOWED_ROOTS=\nTELEGRAM_DOWNLOAD_SUBDIR=Mine\n", encoding="utf-8"
    )

    config._ensure_download_lines(config_path)

    values = config.dotenv_values(config_path)
    assert values["TELEGRAM_ALLOWED_ROOTS"] == ""
    assert values["TELEGRAM_DOWNLOAD_SUBDIR"] == "Mine"
    assert values["TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK"] == "1"


def test_ensure_download_lines_ignores_an_unwritable_file(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(config, "write_values", refuse)

    config._ensure_download_lines(tmp_path / "config.env")  # must not raise


def test_write_values_creates_an_owner_only_file(tmp_path):
    config_path = tmp_path / "nested" / "config.env"

    config.write_values({"TELEGRAM_SESSION_STRING": "abc"}, config_path)

    assert "TELEGRAM_SESSION_STRING=abc" in config_path.read_text(encoding="utf-8")
    if os.name == "posix":
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_write_values_updates_in_place_and_keeps_the_rest(tmp_path):
    config_path = tmp_path / "config.env"
    config_path.write_text(
        "# мои настройки\n"
        "TELEGRAM_API_ID=1\n"
        "TELEGRAM_SESSION_STRING=old\n"
        "TELEGRAM_DEVICE_MODEL=laptop\n",
        encoding="utf-8",
    )

    config.write_values({"TELEGRAM_SESSION_STRING": "new", "TELEGRAM_API_HASH": "hash"}, config_path)

    written = config_path.read_text(encoding="utf-8")
    assert "TELEGRAM_SESSION_STRING=new" in written
    assert "old" not in written
    assert "# мои настройки" in written
    assert "TELEGRAM_DEVICE_MODEL=laptop" in written
    assert "TELEGRAM_API_HASH=hash" in written


def test_write_values_quotes_values_that_need_it(tmp_path):
    config_path = tmp_path / "config.env"

    config.write_values({"TELEGRAM_DEVICE_MODEL": 'Artem "the" laptop'}, config_path)

    reloaded = config_path.read_text(encoding="utf-8")
    assert reloaded.strip() == 'TELEGRAM_DEVICE_MODEL="Artem \\"the\\" laptop"'


def test_written_values_survive_a_round_trip(tmp_path, monkeypatch):
    config_path = tmp_path / "config.env"
    session = _valid_session_string()

    config.write_values(
        {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "hash", "TELEGRAM_SESSION_STRING": session},
        config_path,
    )
    config.load(config_path)

    assert os.environ["TELEGRAM_SESSION_STRING"] == session


def test_session_string_label_suffix_counts_as_session(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK", _valid_session_string())

    config.load(tmp_path / "does-not-exist.env")


def test_invalid_api_id_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "not-a-number")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", _valid_session_string())

    with pytest.raises(config.ConfigError, match="TELEGRAM_API_ID"):
        config.load(tmp_path / "does-not-exist.env")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_warns_when_config_file_is_not_locked_down(tmp_path, capsys):
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="1",
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_STRING=_valid_session_string(),
    )
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)

    config.load(config_path)

    assert "chmod 600" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_no_warning_when_config_file_is_locked_down(tmp_path, capsys):
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="1",
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_STRING=_valid_session_string(),
    )
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    config.load(config_path)

    assert capsys.readouterr().err == ""
