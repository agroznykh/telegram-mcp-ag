"""Pins what the pinned upstream exposes through this package.

Each check runs in a fresh interpreter: importing ``telegram_mcp_ag.server``
registers tools on a process-wide MCP instance and prunes it, which would leak
into every other test module.

When this fails after bumping the upstream SHA in pyproject.toml, read the diff
before touching the expected set: a new name is a new capability the server
would hand to every user, and a tool that writes (to disk or to Telegram) behind
a ``readOnlyHint`` belongs in ``_HIDDEN_UPSTREAM_TOOLS``, not here.
"""

import json
import os
import subprocess
import sys

from telethon.crypto import AuthKey
from telethon.sessions import StringSession

_PROBE = """
import json, os
from telegram_mcp_ag import server
from telegram_mcp import runtime
runtime._apply_exposed_tools_mode()
tools = sorted(t.name for t in runtime.mcp._tool_manager.list_tools())
print(json.dumps({"tools": tools, "transcribe": os.environ.get("TELEGRAM_TRANSCRIBE")}))
"""

# Upstream tools hidden by this package even though read-only mode lets them through.
EXPECTED_HIDDEN = {"transcribe_voice", "open_photo", "wait_for_events", "incoming_feed_status"}

EXPECTED_READ_ONLY_TOOLS = {
    "check_transcription_access",
    "export_contacts",
    "get_admins",
    "get_banned_users",
    "get_blocked_users",
    "get_bot_info",
    "get_chat",
    "get_chatlist_invite",
    "get_chats",
    "get_common_chats",
    "get_contact_chats",
    "get_contact_ids",
    "get_custom_emoji_pack",
    "get_custom_emoji_packs",
    "get_direct_chat_by_contact",
    "get_drafts",
    "get_folder",
    "get_folder_limits",
    "get_folder_snapshot",
    "get_full_chat",
    "get_full_user",
    "get_gif_search",
    "get_history",
    "get_last_interaction",
    "get_me",
    "get_media_info",
    "get_member_admin_status",
    "get_message_context",
    "get_message_link",
    "get_message_reactions",
    "get_message_read_by",
    "get_messages",
    "get_messages_after",
    "get_participants",
    "get_photo_sheet",
    "get_pinned_messages",
    "get_privacy_settings",
    "get_recent_actions",
    "get_scheduled_messages",
    "get_send_as",
    "get_sticker_sets",
    "get_user_photos",
    "get_user_status",
    "inspect_document",
    "list_accounts",
    "list_chats",
    "list_contact_aliases",
    "list_contacts",
    "list_folders",
    "list_forum_topic_icons",
    "list_inline_buttons",
    "list_messages",
    "list_photos",
    "list_topics",
    "preview_custom_emoji",
    "resolve_username",
    "search_contacts",
    "search_global",
    "search_messages",
    "search_public_chats",
    "transcribe_voice_messages",
    "wait_for_new_message",
    "wait_for_settled_message",
}


def _dummy_session_string() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=os.urandom(256))
    return session.save()


def _probe(exposed_tools: str, **extra_env: str) -> dict:
    env = {
        **os.environ,
        "TELEGRAM_API_ID": "1",
        "TELEGRAM_API_HASH": "test-hash",
        "TELEGRAM_SESSION_STRING": _dummy_session_string(),
        "TELEGRAM_EXPOSED_TOOLS": exposed_tools,
        **extra_env,
    }
    done = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_read_only_mode_exposes_exactly_the_reviewed_tools():
    assert set(_probe("read-only")["tools"]) == set(EXPECTED_READ_ONLY_TOOLS)


def test_upstream_tools_we_replace_or_distrust_are_hidden_in_read_only():
    assert not EXPECTED_HIDDEN & set(_probe("read-only")["tools"])


def test_hidden_tools_stay_hidden_when_a_write_tool_is_opted_in():
    tools = set(_probe("read-only+send_message")["tools"])
    assert "send_message" in tools
    assert not EXPECTED_HIDDEN & tools


def test_hidden_tools_stay_hidden_in_all_mode():
    assert not EXPECTED_HIDDEN & set(_probe("all")["tools"])


def test_write_tools_are_absent_in_read_only_mode():
    tools = set(_probe("read-only")["tools"])
    assert not {"send_message", "delete_message", "mark_as_read", "download_media"} & tools


def test_upstream_transcription_cannot_be_turned_back_on():
    assert _probe("read-only", TELEGRAM_TRANSCRIBE="auto")["transcribe"] == "off"
