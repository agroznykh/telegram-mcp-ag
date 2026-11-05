import asyncio
import inspect
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from telethon.crypto import AuthKey
from telethon.sessions import StringSession


def _dummy_session_string() -> str:
    # StringSession().save() returns "" without an auth key; the upstream
    # runtime module exits at import time if it sees an empty session string.
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=os.urandom(256))
    return session.save()


os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("TELEGRAM_SESSION_STRING", _dummy_session_string())

import telegram_mcp_ag
from telegram_mcp_ag import server
from telegram_mcp_ag.config import download_media_enabled
from telegram_mcp_ag.server import (
    SUPPORTED_MEDIA_KINDS,
    _chunk_message_entries,
    _drop_unusable_allowed_roots,
    _format_reset_date,
    _message_media_duration,
    _message_media_kind,
    _quota_exhausted_message,
    _split_entries_by_quota,
    _unsupported_media_message,
)


# ---------------------------------------------------------------------------
# Media kind / duration detection
# ---------------------------------------------------------------------------


def test_message_media_kind_prefers_voice():
    message = SimpleNamespace(voice=object(), video_note=object(), audio=None, video=None, media=None)

    assert _message_media_kind(message) == "voice"


def test_message_media_kind_falls_back_to_media_class_name():
    message = SimpleNamespace(voice=None, video_note=None, audio=None, video=None, media=SimpleNamespace())

    assert _message_media_kind(message) not in SUPPORTED_MEDIA_KINDS


def test_message_media_duration_uses_document_attributes():
    message = SimpleNamespace(document=SimpleNamespace(attributes=[SimpleNamespace(duration=12)]))

    assert _message_media_duration(message) == 12.0


def test_chunk_message_entries_respects_message_and_duration_limits():
    entries = [
        {"message_id": 1, "duration": 30},
        {"message_id": 2, "duration": 30},
        {"message_id": 3, "duration": 70},
        {"message_id": 4, "duration": None},
    ]

    chunks = _chunk_message_entries(entries, max_chunk_duration=60, max_chunk_messages=2)

    assert [[item["message_id"] for item in chunk] for chunk in chunks] == [[1, 2], [3], [4]]


# ---------------------------------------------------------------------------
# Unsupported media messaging
# ---------------------------------------------------------------------------


def test_regular_video_gets_a_specific_message():
    message = _unsupported_media_message("video")

    assert "video" in message.lower()
    assert "voice messages" in message


def test_unknown_document_kind_names_itself_in_the_message():
    message = _unsupported_media_message("MessageMediaDocument")

    assert "MessageMediaDocument" in message


# ---------------------------------------------------------------------------
# Quota helpers
# ---------------------------------------------------------------------------


def test_split_entries_by_quota_keeps_everything_when_unknown():
    entries = [{"message_id": 1}, {"message_id": 2}]

    allowed, skipped = _split_entries_by_quota(entries, quota_remaining=None)

    assert allowed == entries
    assert skipped == []


def test_split_entries_by_quota_skips_the_excess():
    entries = [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]

    allowed, skipped = _split_entries_by_quota(entries, quota_remaining=1)

    assert [e["message_id"] for e in allowed] == [1]
    assert [e["message_id"] for e in skipped] == [2, 3]


def test_split_entries_by_quota_treats_negative_as_zero():
    entries = [{"message_id": 1}]

    allowed, skipped = _split_entries_by_quota(entries, quota_remaining=-5)

    assert allowed == []
    assert skipped == entries


def test_format_reset_date_none_when_unknown():
    assert _format_reset_date(None) is None


def test_format_reset_date_formats_unix_timestamp():
    # 2030-01-01T00:00:00+00:00
    assert _format_reset_date(1893456000) == "2030-01-01T00:00:00+00:00"


def test_quota_exhausted_message_mentions_premium():
    assert "Premium" in _quota_exhausted_message(None)


def test_quota_exhausted_message_includes_reset_date_when_known():
    message = _quota_exhausted_message(1893456000)

    assert "2030-01-01" in message


# ---------------------------------------------------------------------------
# Our reading of TELEGRAM_EXPOSED_TOOLS against upstream's
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    [
        "read-only",
        "read-only+download_media",
        "read-only+send_message,download_media",
        "read-only+download_media-send_message",
        "read-only+send_message",
        "all",
        "all-send_message",
        "all-download_media",
        "READ-ONLY+Download_Media",
    ],
)
def test_download_media_enabled_agrees_with_upstream(mode):
    from telegram_mcp import runtime

    parsed = runtime._parse_exposed_tools_mode(mode)
    exposed = (parsed.base == "all" or "download_media" in parsed.allowlist) and (
        "download_media" not in parsed.exclusions
    )

    assert download_media_enabled(mode) is exposed


# ---------------------------------------------------------------------------
# Download folders that cannot be used
# ---------------------------------------------------------------------------


def test_usable_download_folder_stays(monkeypatch, tmp_path):
    folder = tmp_path / "Telegram"
    monkeypatch.setenv("TELEGRAM_ALLOWED_ROOTS", str(folder))

    _drop_unusable_allowed_roots()

    assert os.environ["TELEGRAM_ALLOWED_ROOTS"] == str(folder)
    assert folder.is_dir()


def test_folder_that_cannot_be_created_is_dropped(monkeypatch, tmp_path, capsys):
    good, bad = tmp_path / "good", tmp_path / "bad"
    real_mkdir = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self == bad:
            raise PermissionError("Operation not permitted")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setenv("TELEGRAM_ALLOWED_ROOTS", f"{good};{bad}")

    _drop_unusable_allowed_roots()

    assert os.environ["TELEGRAM_ALLOWED_ROOTS"] == str(good)
    assert "папка для скачанных файлов недоступна" in capsys.readouterr().err


def test_variable_is_removed_when_no_folder_is_usable(monkeypatch, tmp_path):
    def mkdir(self, *args, **kwargs):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setenv("TELEGRAM_ALLOWED_ROOTS", str(tmp_path / "a"))

    _drop_unusable_allowed_roots()

    assert "TELEGRAM_ALLOWED_ROOTS" not in os.environ


# ---------------------------------------------------------------------------
# download_media: media_id accepted as a string
# ---------------------------------------------------------------------------


def _capture_upstream_download(monkeypatch, answer="Media downloaded to /x."):
    calls = []

    async def upstream(**kwargs):
        calls.append(kwargs)
        return answer

    monkeypatch.setattr(server, "_upstream_download_media", upstream)
    return calls


def test_download_media_takes_a_large_media_id_as_a_string(monkeypatch):
    calls = _capture_upstream_download(monkeypatch)

    asyncio.run(server.download_media(chat_id=5, message_id=7, media_id="4996884764574090643"))

    assert calls[0]["media_id"] == 4996884764574090643
    assert (calls[0]["chat_id"], calls[0]["message_id"]) == (5, 7)


@pytest.mark.parametrize("quoted", ['"4996884764574090643"', "'4996884764574090643'", ' "4996884764574090643" '])
def test_download_media_drops_quotes_around_the_media_id(monkeypatch, quoted):
    calls = _capture_upstream_download(monkeypatch)

    asyncio.run(server.download_media(chat_id=5, message_id=7, media_id=quoted))

    assert calls[0]["media_id"] == 4996884764574090643


def test_download_media_passes_an_integer_or_no_media_id_through(monkeypatch):
    calls = _capture_upstream_download(monkeypatch)

    asyncio.run(server.download_media(chat_id=5, message_id=7, media_id=12))
    asyncio.run(server.download_media(chat_id=5, message_id=7))

    assert [c["media_id"] for c in calls] == [12, None]


@pytest.mark.parametrize("blank", ["", "   ", '""'])
def test_download_media_treats_a_blank_media_id_as_not_given(monkeypatch, blank):
    calls = _capture_upstream_download(monkeypatch)

    asyncio.run(server.download_media(chat_id=5, message_id=7, media_id=blank))

    assert calls[0]["media_id"] is None


def test_download_media_rejects_a_media_id_that_is_not_a_number(monkeypatch):
    calls = _capture_upstream_download(monkeypatch)

    result = asyncio.run(server.download_media(chat_id=5, message_id=7, media_id="photo"))

    assert "media_id must be a number" in result
    assert calls == []


def _download_tool():
    return server.mcp._tool_manager._tools["download_media"]


def test_download_media_schema_offers_media_id_only_as_text():
    # An integer in the schema is what lets a JavaScript client round the id.
    media_id = _download_tool().parameters["properties"]["media_id"]

    assert {option.get("type") for option in media_id["anyOf"]} == {"string", "null"}


def test_download_media_still_accepts_an_exact_integer_from_a_client():
    arguments = _download_tool().fn_metadata.arg_model.model_validate(
        {"chat_id": 5, "message_id": 7, "media_id": 4996884764574090643}
    )

    assert arguments.media_id == "4996884764574090643"


def test_download_media_takes_the_same_arguments_as_upstream():
    # A pin bump that renames or adds a parameter of upstream's download_media must
    # fail here, not as a TypeError on every call.
    ours = set(inspect.signature(server.download_media).parameters)
    upstream = set(inspect.signature(server._upstream_download_media).parameters)

    assert ours == upstream


def test_download_media_keeps_upstream_annotations_and_context():
    tool = _download_tool()

    assert tool.annotations is not None and not tool.annotations.readOnlyHint  # it writes files
    assert "ctx" not in tool.parameters["properties"]
    assert tool.context_kwarg == "ctx"


def test_handshake_reports_this_package_version():
    options = server.mcp._mcp_server.create_initialization_options()

    assert options.server_version == telegram_mcp_ag.__version__


def test_package_version_matches_pyproject():
    # The installers show the pyproject version before installing and __version__ after.
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["version"] == telegram_mcp_ag.__version__
