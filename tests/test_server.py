import os
from types import SimpleNamespace

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

from telegram_mcp_ag.server import (
    SUPPORTED_MEDIA_KINDS,
    _chunk_message_entries,
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
