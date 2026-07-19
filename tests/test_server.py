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

from telegram_mcp_ag.server import _chunk_message_entries, _message_media_duration, _message_media_kind


class ServerHelpersTest(unittest.TestCase):
    def test_message_media_kind_prefers_voice(self) -> None:
        message = SimpleNamespace(voice=object(), video_note=object(), audio=None, video=None, media=None)

        self.assertEqual(_message_media_kind(message), "voice")

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
