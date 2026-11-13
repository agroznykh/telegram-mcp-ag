import os
import stat
import sys

import pytest

from telethon.crypto import AuthKey

from telegram_mcp_ag import config

TELEGRAM_ENV_PREFIX = "TELEGRAM_"


@pytest.fixture(autouse=True)
def clean_telegram_env(monkeypatch):
    saved = dict(os.environ)
    # default_download_root() reads user-dirs.dirs from here on Linux; CI runners
    # set it, and the tests point Path.home() at a temporary folder instead.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    for key in list(os.environ):
        if key.startswith(TELEGRAM_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)
    yield
    # config.load() writes into os.environ directly, which monkeypatch does not
    # track for keys that were absent: put the environment back by hand.
    os.environ.clear()
    os.environ.update(saved)


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
    assert content.startswith("# my note\nOTHER=1\nTELEGRAM_EXPOSED_TOOLS=read-only\n")


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
    # Plain read-only has no download_media, so no download-folder keys.
    assert content == "TELEGRAM_EXPOSED_TOOLS=read-only\n"


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


@pytest.mark.parametrize("name", ["Загрузки, архив", "dir;name", "dir:name"])
def test_download_defaults_skip_a_fallback_path_the_upstream_would_split(monkeypatch, tmp_path, name):
    monkeypatch.setattr(config, "default_download_root", lambda: tmp_path / name / "Telegram")

    values = config.download_defaults()

    assert "TELEGRAM_ALLOWED_ROOTS" not in values
    assert values["TELEGRAM_DOWNLOAD_SUBDIR"] == "Telegram"


def _ensure_default_lines(config_path):
    present = config.dotenv_values(config_path) if config_path.exists() else {}
    config._ensure_default_lines(config_path, present)


def test_first_start_with_downloads_enabled_writes_the_file_once(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_EXPOSED_TOOLS", "all")
    calls = []
    real = config.write_values
    monkeypatch.setattr(config, "write_values", lambda *a, **k: (calls.append(a), real(*a, **k)))
    config_path = tmp_path / "config.env"

    _ensure_default_lines(config_path)

    assert len(calls) == 1
    values = config.dotenv_values(config_path)
    assert values["TELEGRAM_EXPOSED_TOOLS"] == config.DEFAULT_EXPOSED_TOOLS
    assert values["TELEGRAM_DOWNLOAD_SUBDIR"] == "Telegram"


def test_ensure_download_lines_adds_the_missing_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "default_download_root", lambda: tmp_path / "dl" / "Telegram")
    config_path = tmp_path / "config.env"
    config_path.write_text("OTHER=1\nTELEGRAM_EXPOSED_TOOLS=read-only+download_media\n", encoding="utf-8")

    _ensure_default_lines(config_path)

    values = config.dotenv_values(config_path)
    assert values["OTHER"] == "1"
    assert values["TELEGRAM_ALLOWED_ROOTS"] == str(tmp_path / "dl" / "Telegram")
    assert values["TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK"] == "1"
    assert values["TELEGRAM_DOWNLOAD_SUBDIR"] == "Telegram"


def test_ensure_download_lines_keeps_existing_keys_even_when_empty(tmp_path):
    config_path = tmp_path / "config.env"
    config_path.write_text(
        "TELEGRAM_EXPOSED_TOOLS=all\nTELEGRAM_ALLOWED_ROOTS=\nTELEGRAM_DOWNLOAD_SUBDIR=Mine\n",
        encoding="utf-8",
    )

    _ensure_default_lines(config_path)

    values = config.dotenv_values(config_path)
    assert values["TELEGRAM_ALLOWED_ROOTS"] == ""
    assert values["TELEGRAM_DOWNLOAD_SUBDIR"] == "Mine"
    assert values["TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK"] == "1"


def test_ensure_download_lines_ignores_an_unwritable_file(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(config, "write_values", refuse)
    config_path = tmp_path / "config.env"
    config_path.write_text("TELEGRAM_EXPOSED_TOOLS=all\n", encoding="utf-8")

    _ensure_default_lines(config_path)  # must not raise


@pytest.mark.parametrize("mode", ["read-only", "all-download_media", "read-only+send_message", ""])
def test_ensure_download_lines_writes_nothing_without_download_media(tmp_path, mode):
    config_path = tmp_path / "config.env"
    config_path.write_text(f"TELEGRAM_EXPOSED_TOOLS={mode}\n", encoding="utf-8")

    _ensure_default_lines(config_path)

    assert config_path.read_text(encoding="utf-8") == f"TELEGRAM_EXPOSED_TOOLS={mode}\n"


def test_ensure_download_lines_follows_the_environment_over_the_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.env"
    config_path.write_text("TELEGRAM_EXPOSED_TOOLS=read-only\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_EXPOSED_TOOLS", "all")

    _ensure_default_lines(config_path)

    assert "TELEGRAM_DOWNLOAD_SUBDIR" in config.dotenv_values(config_path)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("read-only", False),
        ("read-only+download_media", True),
        ("READ-ONLY+Download_Media", True),
        ("read-only+send_message,download_media", True),
        ("read-only+download_media-send_message", True),
        ("all", True),
        ("all-send_message", True),
        ("all-download_media", False),
        ("read-only+download_media-download_media", False),
        ("read-only+send_message", False),
        ("allx", False),
        ("nonsense", False),
        ("", False),
        (None, False),
    ],
)
def test_download_media_enabled(mode, expected):
    assert config.download_media_enabled(mode) is expected


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


# ---------------------------------------------------------------------------
# Account labels and per-account session keys
# ---------------------------------------------------------------------------


def test_normalize_label_matches_upstream():
    from telegram_mcp.account_labels import canonical_account_label

    for label in ("Work", " my-Work ", "A_b-C", "default"):
        assert config.normalize_label(label) == canonical_account_label(label)


@pytest.mark.parametrize("label", ["work", "Work", "my-work", "a_1", "x" * 32, "default"])
def test_validate_label_accepts_allowed_labels(label):
    assert config.validate_label(label) == config.normalize_label(label)


@pytest.mark.parametrize("label", ["", "  ", "рабочий", "a b", "a" * 33, "w;rm", "a$b", "a/b", "w\nx", "é"])
def test_validate_label_rejects_the_rest_with_a_hint(label):
    with pytest.raises(config.ConfigError) as exc:
        config.validate_label(label)

    assert "латин" in str(exc.value)


def test_default_label_is_reserved_for_the_unnamed_key():
    assert config.validate_label("Default") == "default"
    with pytest.raises(config.ConfigError):
        config.validate_label("default", allow_default=False)


def test_session_key_for_default_and_labels():
    assert config.session_key("default") == "TELEGRAM_SESSION_STRING"
    assert config.session_key("my-work") == "TELEGRAM_SESSION_STRING_MY_WORK"
    assert config.session_key("Work") == "TELEGRAM_SESSION_STRING_WORK"


def test_configured_labels_skip_blank_and_invalid_and_count_name_keys():
    env = {
        "TELEGRAM_SESSION_STRING": "a",
        "TELEGRAM_SESSION_STRING_WORK": "b",
        "TELEGRAM_SESSION_STRING_OLD": "  ",
        "TELEGRAM_SESSION_STRING_bad;label": "c",
        "TELEGRAM_SESSION_STRING_": "d",
        "TELEGRAM_SESSION_NAME_Files": "e",
        "TELEGRAM_SESSION_STRING_Work": "dup",
        "TELEGRAM_SESSION_STRINGS": "pool, not an account",
        "TELEGRAM_API_ID": "1",
    }

    assert config.configured_labels(env) == ["default", "files", "work"]
    assert config.is_multi(env)
    assert not config.is_multi({"TELEGRAM_SESSION_STRING": "a", "TELEGRAM_SESSION_STRING_X": ""})
    assert config.configured_labels({}) == []


def test_default_account_reads_the_label_and_ignores_blank_or_unknown(capsys):
    env = {"TELEGRAM_SESSION_STRING": "a", "TELEGRAM_SESSION_STRING_WORK": "b"}

    assert config.default_account({**env, "TELEGRAM_DEFAULT_ACCOUNT": " Work "}) == "work"
    assert config.default_account({**env, "TELEGRAM_DEFAULT_ACCOUNT": "default"}) == "default"
    assert config.default_account(env) is None
    assert config.default_account({**env, "TELEGRAM_DEFAULT_ACCOUNT": "  "}) is None
    assert capsys.readouterr().err == ""

    assert config.default_account({**env, "TELEGRAM_DEFAULT_ACCOUNT": "nobody"}) is None
    assert config.default_account({**env, "TELEGRAM_DEFAULT_ACCOUNT": "bad;label"}) is None
    assert capsys.readouterr().err.count("Warning") == 2


def test_load_drops_invalid_labels_and_a_duplicate_of_the_unnamed_key(monkeypatch, tmp_path, capsys):
    good, other = _valid_session_string(2), _valid_session_string(4)
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="1",
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_STRING=good,
        TELEGRAM_SESSION_STRING_DEFAULT=other,
        TELEGRAM_SESSION_STRING_WORK=other,
        **{"TELEGRAM_SESSION_STRING_bad;label": other, "TELEGRAM_SESSION_NAME_ёж": "x"},
    )

    config_path.chmod(0o600)  # else the permissions warning joins the count below

    config.load(config_path)

    assert os.environ["TELEGRAM_SESSION_STRING"] == good
    assert os.environ["TELEGRAM_SESSION_STRING_WORK"] == other
    assert "TELEGRAM_SESSION_STRING_DEFAULT" not in os.environ
    assert "TELEGRAM_SESSION_STRING_bad;label" not in os.environ
    assert "TELEGRAM_SESSION_NAME_ёж" not in os.environ
    err = capsys.readouterr().err
    assert err.count("Warning") == 3
    assert other not in err and good not in err


def test_invalid_labels_alone_leave_no_session(monkeypatch, tmp_path):
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="1",
        TELEGRAM_API_HASH="hash",
        **{"TELEGRAM_SESSION_STRING_bad;label": _valid_session_string()},
    )

    with pytest.raises(config.MissingSessionError):
        config.load(config_path)


def test_default_label_key_alone_is_still_an_account(tmp_path):
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="1",
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_STRING_DEFAULT=_valid_session_string(),
    )

    config.load(config_path)

    assert config.configured_labels(os.environ) == ["default"]


def _three_keys_file(tmp_path):
    path = tmp_path / "config.env"
    path.write_text(
        "# my settings\n"
        "TELEGRAM_API_ID=1\n"
        "TELEGRAM_SESSION_STRING=sess-default\n"
        "\n"
        "TELEGRAM_SESSION_STRING_WORK=sess-work\n"
        "TELEGRAM_SESSION_STRING_OLD=\n"
        "TELEGRAM_EXPOSED_TOOLS=read-only\n",
        encoding="utf-8",
    )
    return path


def test_acceptance_three_keys(tmp_path):
    from dotenv import dotenv_values

    path = _three_keys_file(tmp_path)

    assert config.configured_labels(dotenv_values(path)) == ["default", "work"]
    assert config.clear_session(path, label="work") is True
    text = path.read_text(encoding="utf-8")
    assert "TELEGRAM_SESSION_STRING=sess-default" in text
    assert "sess-work" not in text and "_WORK" not in text
    assert "# my settings" in text and "TELEGRAM_EXPOSED_TOOLS=read-only" in text
    assert "TELEGRAM_SESSION_STRING_OLD=" in text  # another account's (blank) key stays


def test_clear_session_without_label_still_drops_everything(tmp_path):
    path = _three_keys_file(tmp_path)

    assert config.clear_session(path) is True
    text = path.read_text(encoding="utf-8")
    assert "TELEGRAM_SESSION" not in text
    assert "TELEGRAM_API_ID=1" in text


def test_clear_session_with_unknown_label_changes_nothing(tmp_path):
    path = _three_keys_file(tmp_path)
    before = path.read_text(encoding="utf-8")

    assert config.clear_session(path, label="nobody") is False
    assert path.read_text(encoding="utf-8") == before


def test_clear_session_by_label_matches_any_spelling_of_the_key(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("TELEGRAM_SESSION_STRING_Work=a\nTELEGRAM_SESSION_NAME_WORK=b\nTELEGRAM_SESSION_STRING=c\n", encoding="utf-8")

    config.clear_session(path, label="work")

    assert path.read_text(encoding="utf-8") == "TELEGRAM_SESSION_STRING=c\n"


def test_write_session_changes_only_its_own_key(tmp_path):
    path = _three_keys_file(tmp_path)

    config.write_session("work", "new-work", path)
    config.write_session("fresh", "sess-fresh", path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.index("TELEGRAM_SESSION_STRING_WORK=new-work") == 4  # in place
    assert "TELEGRAM_SESSION_STRING=sess-default" in lines
    assert "TELEGRAM_SESSION_STRING_FRESH=sess-fresh" in lines
    assert "# my settings" in lines and "" in lines
    assert sum("WORK" in line for line in lines) == 1
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_session_replaces_a_hand_written_spelling_instead_of_adding(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("TELEGRAM_SESSION_STRING_work=dead\nTELEGRAM_SESSION_STRING_DEFAULT=dead2\n", encoding="utf-8")

    config.write_session("work", "alive", path)
    config.write_session("default", "alive2", path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert sorted(lines) == ["TELEGRAM_SESSION_STRING=alive2", "TELEGRAM_SESSION_STRING_WORK=alive"]


def test_write_session_creates_an_owner_only_file(tmp_path):
    path = tmp_path / "nested" / "config.env"

    config.write_session("default", "s", path)

    assert path.read_text(encoding="utf-8") == "TELEGRAM_SESSION_STRING=s\n"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_session_rejects_a_bad_label_and_writes_nothing(tmp_path):
    path = _three_keys_file(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(config.ConfigError):
        config.write_session("bad label", "x", path)

    assert path.read_text(encoding="utf-8") == before


def test_rename_default_leaves_exactly_one_personal_key(tmp_path):
    path = _three_keys_file(tmp_path)

    config.rename_session("default", "Personal", path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.index("TELEGRAM_SESSION_STRING_PERSONAL=sess-default") == 2  # where the old one stood
    assert not any(line.startswith("TELEGRAM_SESSION_STRING=") for line in lines)
    assert "TELEGRAM_SESSION_STRING_WORK=sess-work" in lines
    assert "# my settings" in lines and "TELEGRAM_EXPOSED_TOOLS=read-only" in lines
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rename_moves_the_default_account_setting_along(tmp_path):
    path = _three_keys_file(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "TELEGRAM_DEFAULT_ACCOUNT=default\n", encoding="utf-8")

    config.rename_session("default", "personal", path)

    assert "TELEGRAM_DEFAULT_ACCOUNT=personal" in path.read_text(encoding="utf-8").splitlines()


def test_rename_keeps_the_default_account_setting_when_it_names_another(tmp_path):
    path = _three_keys_file(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "TELEGRAM_DEFAULT_ACCOUNT=work\n", encoding="utf-8")

    config.rename_session("default", "personal", path)

    assert "TELEGRAM_DEFAULT_ACCOUNT=work" in path.read_text(encoding="utf-8").splitlines()


def test_rename_refuses_without_a_source_or_over_an_existing_account(tmp_path):
    path = _three_keys_file(tmp_path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(config.ConfigError):
        config.rename_session("nobody", "x", path)
    with pytest.raises(config.ConfigError):
        config.rename_session("default", "work", path)
    with pytest.raises(config.ConfigError):
        config.rename_session("work", "default", path)
    with pytest.raises(config.ConfigError):
        config.rename_session("default", "bad label", path)

    assert path.read_text(encoding="utf-8") == before


def test_rename_over_a_blank_leftover_of_the_new_label_works(tmp_path):
    path = _three_keys_file(tmp_path)

    config.rename_session("default", "old", path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if "_OLD" in line] == ["TELEGRAM_SESSION_STRING_OLD=sess-default"]


def test_session_strings_survive_rename_and_write_with_odd_characters(tmp_path):
    from dotenv import dotenv_values

    path = tmp_path / "config.env"
    session = _valid_session_string()
    config.write_session("default", session, path)

    config.rename_session("default", "me", path)

    assert dotenv_values(path)["TELEGRAM_SESSION_STRING_ME"] == session


def test_set_default_account_sets_changes_and_removes(tmp_path):
    path = _three_keys_file(tmp_path)

    config.set_default_account("Work", path)
    assert "TELEGRAM_DEFAULT_ACCOUNT=work" in path.read_text(encoding="utf-8")
    config.set_default_account("default", path)
    text = path.read_text(encoding="utf-8")
    assert text.count("TELEGRAM_DEFAULT_ACCOUNT") == 1 and "TELEGRAM_DEFAULT_ACCOUNT=default" in text

    config.set_default_account(None, path)
    text = path.read_text(encoding="utf-8")
    assert "TELEGRAM_DEFAULT_ACCOUNT" not in text
    assert "# my settings" in text and "TELEGRAM_SESSION_STRING_WORK=sess-work" in text
    config.set_default_account(None, path)  # nothing to remove: not an error


def test_set_default_account_refuses_an_account_without_a_session(tmp_path):
    path = _three_keys_file(tmp_path)

    for label in ("nobody", "old", "bad label"):
        with pytest.raises(config.ConfigError):
            config.set_default_account(label, path)
    assert "TELEGRAM_DEFAULT_ACCOUNT" not in path.read_text(encoding="utf-8")


def test_blank_default_account_in_the_environment_is_not_set(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_DEFAULT_ACCOUNT", "")
    env = {**os.environ, "TELEGRAM_SESSION_STRING": "a", "TELEGRAM_SESSION_STRING_WORK": "b"}

    assert config.default_account(env) is None


def test_default_key_is_kept_when_the_unnamed_string_is_unreadable(tmp_path, capsys):
    good = _valid_session_string()
    config_path = _write_config(
        tmp_path / "config.env",
        TELEGRAM_API_ID="1",
        TELEGRAM_API_HASH="hash",
        TELEGRAM_SESSION_STRING="garbage",
        TELEGRAM_SESSION_STRING_DEFAULT=good,
    )
    config_path.chmod(0o600)

    config.load(config_path)

    assert os.environ["TELEGRAM_SESSION_STRING_DEFAULT"] == good
    assert "TELEGRAM_SESSION_STRING" not in os.environ


def test_read_env_overlays_non_blank_environment_and_ignores_blank(tmp_path, monkeypatch):
    path = _three_keys_file(tmp_path)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_WORK", "")
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_EXTRA", "from-env")
    monkeypatch.setenv("TELEGRAM_API_ID", "7")

    env = config.read_env(path)

    assert env["TELEGRAM_SESSION_STRING_WORK"] == "sess-work"
    assert env["TELEGRAM_SESSION_STRING_EXTRA"] == "from-env"
    assert env["TELEGRAM_API_ID"] == "7"
    assert config.read_env(tmp_path / "missing.env").get("TELEGRAM_SESSION_STRING") is None


def test_set_default_and_rename_see_accounts_set_in_the_environment(tmp_path, monkeypatch):
    path = _three_keys_file(tmp_path)
    monkeypatch.setenv("TELEGRAM_SESSION_STRING_CLOUD", "from-env")

    config.set_default_account("cloud", path)
    assert "TELEGRAM_DEFAULT_ACCOUNT=cloud" in path.read_text(encoding="utf-8")

    with pytest.raises(config.ConfigError):
        config.rename_session("default", "cloud", path)


def test_rename_refuses_an_account_kept_in_a_session_file(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("TELEGRAM_SESSION_STRING_WORK=a\nTELEGRAM_SESSION_NAME_WORK=file-session\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    with pytest.raises(config.ConfigError):
        config.rename_session("work", "job", path)

    assert path.read_text(encoding="utf-8") == before


def test_write_session_applies_extra_values_in_the_same_write(tmp_path):
    path = tmp_path / "config.env"
    path.write_text("TELEGRAM_API_ID=1\n# note\n", encoding="utf-8")

    config.write_session("work", "s", path, extra_values={"TELEGRAM_API_ID": "2", "TELEGRAM_API_HASH": "h"})

    assert path.read_text(encoding="utf-8").splitlines() == [
        "TELEGRAM_API_ID=2",
        "# note",
        "TELEGRAM_SESSION_STRING_WORK=s",
        "TELEGRAM_API_HASH=h",
    ]
