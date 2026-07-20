import os
import stat
import sys

import pytest

from telegram_mcp_ag import config

TELEGRAM_ENV_PREFIX = "TELEGRAM_"


@pytest.fixture(autouse=True)
def clean_telegram_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(TELEGRAM_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def _write_config(path, **values):
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return path


def test_loads_missing_vars_from_file(tmp_path):
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="123",
        TELEGRAM_API_HASH="hash-from-file",
        TELEGRAM_SESSION_STRING="session-from-file",
    )

    config.load(config_path)

    assert os.environ["TELEGRAM_API_ID"] == "123"
    assert os.environ["TELEGRAM_API_HASH"] == "hash-from-file"
    assert os.environ["TELEGRAM_SESSION_STRING"] == "session-from-file"


def test_env_vars_take_priority_over_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash-from-env")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "session-from-env")
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="999",
        TELEGRAM_API_HASH="hash-from-file",
        TELEGRAM_SESSION_STRING="session-from-file",
    )

    config.load(config_path)

    assert os.environ["TELEGRAM_API_HASH"] == "hash-from-env"
    assert os.environ["TELEGRAM_SESSION_STRING"] == "session-from-env"


def test_missing_file_is_not_an_error_when_env_is_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "session")

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

    with pytest.raises(config.ConfigError, match="session"):
        config.load(tmp_path / "does-not-exist.env")


def test_session_string_label_suffix_counts_as_session(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK", "session")

    config.load(tmp_path / "does-not-exist.env")


def test_invalid_api_id_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_API_ID", "not-a-number")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING", "session")

    with pytest.raises(config.ConfigError, match="TELEGRAM_API_ID"):
        config.load(tmp_path / "does-not-exist.env")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions only")
def test_warns_when_config_file_is_not_locked_down(tmp_path, capsys):
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="1",
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_STRING="session",
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
        TELEGRAM_SESSION_STRING="session",
    )
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    config.load(config_path)

    assert capsys.readouterr().err == ""
