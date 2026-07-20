import os
from types import SimpleNamespace
import unittest

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


class ServerHelpersTest(unittest.TestCase):
    def test_message_media_kind_prefers_voice(self) -> None:
        message = SimpleNamespace(voice=object(), video_note=object(), audio=None, video=None, media=None)

        self.assertEqual(_message_media_kind(message), "voice")

    def test_message_media_kind_falls_back_to_media_class_name(self) -> None:
        message = SimpleNamespace(
            voice=None, video_note=None, audio=None, video=None, media=SimpleNamespace()
        )

        self.assertNotIn(_message_media_kind(message), SUPPORTED_MEDIA_KINDS)

    def test_message_media_duration_uses_document_attributes(self) -> None:
        message = SimpleNamespace(document=SimpleNamespace(attributes=[SimpleNamespace(duration=12)]))

        self.assertEqual(_message_media_duration(message), 12.0)

    def test_chunk_message_entries_respects_message_and_duration_limits(self) -> None:
        entries = [
            {"message_id": 1, "duration": 30},
            {"message_id": 2, "duration": 30},
            {"message_id": 3, "duration": 70},
            {"message_id": 4, "duration": None},
        ]

        chunks = _chunk_message_entries(entries, max_chunk_duration=60, max_chunk_messages=2)

        self.assertEqual(
            [[item["message_id"] for item in chunk] for chunk in chunks], [[1, 2], [3], [4]]
        )


class UnsupportedMediaMessageTest(unittest.TestCase):
    def test_regular_video_gets_a_specific_message(self) -> None:
        message = _unsupported_media_message("video")

        self.assertIn("video", message.lower())
        self.assertIn("voice messages", message)

    def test_unknown_document_kind_names_itself_in_the_message(self) -> None:
        message = _unsupported_media_message("MessageMediaDocument")

        self.assertIn("MessageMediaDocument", message)


class QuotaHelpersTest(unittest.TestCase):
    def test_split_entries_by_quota_keeps_everything_when_unknown(self) -> None:
        entries = [{"message_id": 1}, {"message_id": 2}]

        allowed, skipped = _split_entries_by_quota(entries, quota_remaining=None)

        self.assertEqual(allowed, entries)
        self.assertEqual(skipped, [])

    def test_split_entries_by_quota_skips_the_excess(self) -> None:
        entries = [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]

        allowed, skipped = _split_entries_by_quota(entries, quota_remaining=1)

        self.assertEqual([e["message_id"] for e in allowed], [1])
        self.assertEqual([e["message_id"] for e in skipped], [2, 3])

    def test_split_entries_by_quota_treats_negative_as_zero(self) -> None:
        entries = [{"message_id": 1}]

        allowed, skipped = _split_entries_by_quota(entries, quota_remaining=-5)

        self.assertEqual(allowed, [])
        self.assertEqual(skipped, entries)

    def test_format_reset_date_none_when_unknown(self) -> None:
        self.assertIsNone(_format_reset_date(None))

    def test_format_reset_date_formats_unix_timestamp(self) -> None:
        # 2030-01-01T00:00:00+00:00
        self.assertEqual(_format_reset_date(1893456000), "2030-01-01T00:00:00+00:00")

    def test_quota_exhausted_message_mentions_premium(self) -> None:
        self.assertIn("Premium", _quota_exhausted_message(None))

    def test_quota_exhausted_message_includes_reset_date_when_known(self) -> None:
        message = _quota_exhausted_message(1893456000)

        self.assertIn("2030-01-01", message)
