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
    # Claude Desktop runs the .mcpb bundle with every user_config field
    # substituted, so a field the user left blank arrives as an empty string.
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
