"""Telegram MCP launcher with safe, read-only voice transcription tools."""

import asyncio
import importlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Union

from telethon.errors.rpcerrorlist import (
    AuthKeyInvalidError,
    AuthKeyUnregisteredError,
    PremiumAccountRequiredError,
    SessionRevokedError,
)
from telethon.tl import functions

from telegram_mcp_ag import config as _config

# Login-only mode: credentials are there but no session is. Rather than dying
# and sending the user to the terminal, the server comes up with only the QR
# login tools (see login.py). telegram_mcp.runtime builds its clients at import
# time and exits when no session is configured, so it is handed a throwaway file
# session here; _install_login_client() swaps it for an in-memory one below,
# before anything connects, so no auth key is ever written to disk.
_LOGIN_MODE = False
_LOGIN_TMPDIR = None

try:
    _config.load()
except _config.MissingSessionError:
    _LOGIN_MODE = True
    _LOGIN_TMPDIR = tempfile.mkdtemp(prefix="telegram-mcp-ag-bootstrap-")
    os.environ["TELEGRAM_SESSION_NAME"] = str(Path(_LOGIN_TMPDIR) / "bootstrap")
except _config.ConfigError as _config_error:
    print(f"telegram-mcp-ag: {_config_error}", file=sys.stderr)
    sys.exit(1)

from telegram_mcp import runner as _runner
from telegram_mcp import runtime as _runtime
from telegram_mcp.runtime import (
    ToolAnnotations,
    ensure_connected,
    get_client,
    get_sender_name,
    json_serializer,
    log_and_format_error,
    mcp,
    resolve_entity,
    sanitize_user_content,
    validate_id,
    with_account,
)


def _install_login_client() -> None:
    """Replace the throwaway file session with an in-memory one.

    Called before any connection is made, so the bootstrap ``.session`` file
    stays empty and can be deleted right away. Once the QR login succeeds this
    same client is authorized in place, which is what lets the reading tools
    start working without restarting the extension.
    """
    from telethon.sessions import StringSession

    for label, client in list(_runtime.clients.items()):
        try:
            client.session.close()
        except Exception:
            pass
        _runtime.clients[label] = _runtime._build_client(StringSession(), label)

    shutil.rmtree(_LOGIN_TMPDIR, ignore_errors=True)
    os.environ.pop("TELEGRAM_SESSION_NAME", None)


_LOGIN_TOOLS: list = []
_HIDDEN_TOOLS: dict = {}

if _LOGIN_MODE:
    import nest_asyncio

    from telegram_mcp_ag import login as _login

    _install_login_client()
    _LOGIN_TOOLS = _login.register(mcp, ToolAnnotations, get_client)
    _login.set_activation_hook(lambda: _activate_reading_tools())


# Revoking this device's session in Telegram (Settings -> Devices) or ending
# all sessions at once doesn't raise anything a client sees directly: every
# upstream tool catches its own exceptions and calls log_and_format_error(),
# which -- without a user_message -- returns an opaque
# "An error occurred (code: CHAT-ERR-606)" with zero indication that the fix
# is a fresh login, not a retry. Patching log_and_format_error to recognize
# these specific errors and fill in the exact --relogin command fixes this
# for every tool and every MCP client at once (Claude, Codex, ChatGPT...),
# rather than depending on a skill being installed or its description
# happening to match the user's phrasing.
#
# `telegram_mcp.tools.*` each did `from telegram_mcp.runtime import *` at
# their own import time, so every submodule holds its own separate name
# binding for log_and_format_error -- patching telegram_mcp.runtime alone
# would not reach any of them. Best effort: a module missing the attribute
# (future upstream restructuring) is skipped rather than failing the server.
_RELOGIN_SESSION_ERRORS = (AuthKeyUnregisteredError, SessionRevokedError, AuthKeyInvalidError)

_RELOGIN_CMD_MAC_LINUX = (
    "curl -fsSL https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.sh"
    " | bash -s -- --relogin"
)
_RELOGIN_CMD_WINDOWS = (
    '$f = "$env:TEMP\\telegram-mcp-ag-install.ps1"; '
    "iwr 'https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.ps1'"
    " -OutFile $f; & $f -Relogin"
)


def _relogin_instructions() -> str:
    system = platform.system()
    if system == "Darwin" or system == "Linux":
        return f"Выполните на этой машине в терминале: {_RELOGIN_CMD_MAC_LINUX}"
    if system == "Windows":
        return f"Выполните на этой машине в PowerShell: {_RELOGIN_CMD_WINDOWS}"
    return (
        "Не удалось определить ОС этой машины, выберите нужную команду -- "
        f"macOS/Linux (терминал): {_RELOGIN_CMD_MAC_LINUX} -- "
        f"Windows (PowerShell): {_RELOGIN_CMD_WINDOWS}"
    )


def _relogin_user_message(error: BaseException) -> Optional[str]:
    if isinstance(error, _RELOGIN_SESSION_ERRORS):
        return (
            "Сессия Telegram для этого аккаунта отозвана (устройство удалено в "
            "Telegram -> Настройки -> Устройства, либо разлогинены все сеансы разом) "
            f"-- нужно подключить аккаунт заново. {_relogin_instructions()}"
        )
    return None


def _patch_log_and_format_error():
    original = _runtime.log_and_format_error

    def patched(function_name, error, prefix=None, user_message=None, **kwargs):
        if user_message is None:
            user_message = _relogin_user_message(error)
        return original(function_name, error, prefix=prefix, user_message=user_message, **kwargs)

    _runtime.log_and_format_error = patched
    for _mod_name in (
        "accounts",
        "chats",
        "contacts",
        "events",
        "folders",
        "groups",
        "media",
        "messages",
        "profile",
    ):
        try:
            _mod = importlib.import_module(f"telegram_mcp.tools.{_mod_name}")
        except ImportError:
            continue
        if hasattr(_mod, "log_and_format_error"):
            _mod.log_and_format_error = patched

    return patched


log_and_format_error = _patch_log_and_format_error()


DEFAULT_MAX_CHUNK_DURATION = 180
DEFAULT_MAX_CHUNK_MESSAGES = 6
DEFAULT_MAX_RUNTIME_SECONDS = 100
DEFAULT_REQUEST_TIMEOUT = 30
# Telegram doesn't always have a transcription ready by the first poll, even
# after the request succeeds (`pending=True` in the response). 2 retries at
# 1.5s wasn't always enough in practice -- short clips have come back still
# pending after ~8s of waiting; 4 retries at 2s gives more room before the
# caller has to poll again itself.
DEFAULT_RETRY_COUNT = 4
DEFAULT_RETRY_DELAY = 2.0

# What Telegram's native transcription actually handles today. Regular videos
# and plain documents (even ones that happen to contain audio) come back as
# some other MessageMedia* kind and are rejected below with a clear message
# instead of being sent to Telegram and failing there.
SUPPORTED_MEDIA_KINDS = frozenset({"voice", "video_note", "audio"})

# Telegram has no RPC to ask "how many free transcriptions are left": the
# trial_remains_num/trial_remains_until_date fields only appear in the
# response of an actual messages.transcribeAudio call. Telegram's own TDLib
# client does the same thing we do here: remember the last real answer for
# the lifetime of the session and treat it as unknown until then.
_ACCOUNT_STATE: dict[int, dict] = {}


def _account_state(cl) -> dict:
    return _ACCOUNT_STATE.setdefault(
        id(cl),
        {
            "is_premium": None,
            "trial_remains_num": None,
            "trial_remains_until_date": None,
            "weekly_quota": None,
            "max_trial_duration_seconds": None,
        },
    )


async def _get_is_premium(cl) -> bool:
    state = _account_state(cl)
    if state["is_premium"] is None:
        me = await cl.get_me()
        state["is_premium"] = bool(getattr(me, "premium", False))
    return state["is_premium"]


async def _get_trial_limits(cl) -> tuple[Optional[int], Optional[int]]:
    """Return (weekly_quota, max_trial_duration_seconds) from help.getAppConfig.

    These are the same values Telegram's official clients read to show "N
    free transcriptions per week, up to M seconds each" — global ceilings,
    not this account's remaining count.
    """
    state = _account_state(cl)
    if state["weekly_quota"] is not None or state["max_trial_duration_seconds"] is not None:
        return state["weekly_quota"], state["max_trial_duration_seconds"]

    result = await cl(functions.help.GetAppConfigRequest(hash=0))
    config = getattr(result, "config", None)
    for entry in getattr(config, "value", None) or []:
        key = getattr(entry, "key", None)
        raw_value = getattr(getattr(entry, "value", None), "value", None)
        if key == "transcribe_audio_trial_weekly_number" and raw_value is not None:
            state["weekly_quota"] = int(raw_value)
        elif key == "transcribe_audio_trial_duration_max" and raw_value is not None:
            state["max_trial_duration_seconds"] = int(raw_value)

    return state["weekly_quota"], state["max_trial_duration_seconds"]


def _record_trial_state(cl, trial_remains_num, trial_remains_until_date) -> None:
    if trial_remains_num is None and trial_remains_until_date is None:
        return
    state = _account_state(cl)
    state["trial_remains_num"] = trial_remains_num
    state["trial_remains_until_date"] = trial_remains_until_date


def _format_reset_date(trial_remains_until_date: Optional[int]) -> Optional[str]:
    if trial_remains_until_date is None:
        return None
    return datetime.fromtimestamp(trial_remains_until_date, tz=timezone.utc).isoformat()


def _quota_exhausted_message(trial_remains_until_date: Optional[int]) -> str:
    base = "Skipped: the free weekly transcription quota for this account is used up."
    reset_at = _format_reset_date(trial_remains_until_date)
    if reset_at is None:
        return f"{base} Telegram Premium removes this limit."
    return f"{base} It resets at {reset_at} (UTC). Telegram Premium removes this limit."


def _unsupported_media_message(media_kind: str) -> str:
    if media_kind == "video":
        return (
            "Regular videos aren't supported for transcription "
            "(voice messages, video circles, and audio files are)."
        )
    return (
        f"Message media type '{media_kind}' isn't supported for transcription "
        "(only voice messages, video circles, and audio files are)."
    )


def _split_entries_by_quota(
    entries: list[dict], quota_remaining: Optional[int]
) -> tuple[list[dict], list[dict]]:
    """Split entries into (allowed, skipped) given a known remaining trial quota.

    ``quota_remaining=None`` means the remaining count isn't known yet, so
    nothing is preemptively skipped.
    """
    if quota_remaining is None:
        return entries, []
    quota_remaining = max(0, quota_remaining)
    return entries[:quota_remaining], entries[quota_remaining:]


def _message_media_kind(msg) -> str:
    if getattr(msg, "voice", None) is not None:
        return "voice"
    if getattr(msg, "video_note", None) is not None:
        return "video_note"
    if getattr(msg, "audio", None) is not None:
        return "audio"
    if getattr(msg, "video", None) is not None:
        return "video"
    if getattr(msg, "media", None) is not None:
        return type(msg.media).__name__
    return ""


def _message_media_duration(msg) -> Optional[float]:
    document = getattr(msg, "document", None)
    for attr in getattr(document, "attributes", []) or []:
        duration = getattr(attr, "duration", None)
        if duration is not None:
            return float(duration)
    return None


def _transcription_payload(cl, msg, result) -> dict:
    text = getattr(result, "text", "") or ""
    payload = {
        "message_id": msg.id,
        "sender": get_sender_name(msg),
        "date": getattr(msg, "date", None),
        "media": _message_media_kind(msg),
        "duration": _message_media_duration(msg),
        "transcription_id": getattr(result, "transcription_id", None),
        "pending": bool(getattr(result, "pending", False)),
        "text": sanitize_user_content(text) if text else "",
    }

    trial_remains_num = getattr(result, "trial_remains_num", None)
    trial_remains_until_date = getattr(result, "trial_remains_until_date", None)
    _record_trial_state(cl, trial_remains_num, trial_remains_until_date)

    if trial_remains_num is not None:
        payload["trial_remains_num"] = trial_remains_num
    if trial_remains_until_date is not None:
        payload["trial_remains_until_date"] = trial_remains_until_date

    return payload


def _chunk_message_entries(
    entries: list[dict],
    max_chunk_duration: float,
    max_chunk_messages: int,
) -> list[list[dict]]:
    chunks = []
    current = []
    current_duration = 0.0

    for entry in entries:
        duration = entry.get("duration")
        duration = 0.0 if duration is None else float(duration)

        should_start_new = current and (
            len(current) >= max_chunk_messages
            or current_duration + duration > max_chunk_duration
        )
        if should_start_new:
            chunks.append(current)
            current = []
            current_duration = 0.0

        current.append(entry)
        current_duration += duration

    if current:
        chunks.append(current)

    return chunks


async def _transcribe_one_message(
    cl,
    entity,
    msg,
    retry_count: int,
    retry_delay: float,
    request_timeout: float,
) -> dict:
    last_payload = None
    for attempt in range(retry_count + 1):
        result = await asyncio.wait_for(
            cl(functions.messages.TranscribeAudioRequest(peer=entity, msg_id=msg.id)),
            timeout=request_timeout,
        )
        last_payload = _transcription_payload(cl, msg, result)
        last_payload["attempt"] = attempt + 1
        if not last_payload["pending"] or last_payload["text"]:
            break
        if attempt < retry_count:
            await asyncio.sleep(retry_delay)

    return last_payload


@mcp.tool(
    annotations=ToolAnnotations(
        title="Check Transcription Access",
        openWorldHint=True,
        readOnlyHint=True,
    )
)
@with_account(readonly=True)
async def check_transcription_access(account: str = None) -> str:
    """Report Premium status and remaining free voice-transcription quota.

    Telegram does not expose a way to query the remaining weekly trial count
    on its own: it is only revealed in the response of an actual
    transcription attempt. Until this account has made one in this session,
    ``quota_known`` is ``false`` and only the global weekly ceiling is shown.
    """
    try:
        cl = get_client(account)
        await ensure_connected(cl)

        is_premium = await _get_is_premium(cl)
        weekly_quota, max_trial_duration_seconds = await _get_trial_limits(cl)

        result = {
            "is_premium": is_premium,
            "weekly_trial_quota": weekly_quota,
            "max_trial_duration_seconds": max_trial_duration_seconds,
        }

        if is_premium:
            result["note"] = "Premium account: no weekly quota or duration limit applies."
        else:
            state = _account_state(cl)
            trial_remains_num = state["trial_remains_num"]
            trial_remains_until_date = state["trial_remains_until_date"]
            result["quota_known"] = trial_remains_num is not None
            if result["quota_known"]:
                result["trial_remains_num"] = trial_remains_num
                result["trial_remains_until_date"] = trial_remains_until_date
                result["trial_remains_until_date_iso"] = _format_reset_date(
                    trial_remains_until_date
                )
            else:
                result["note"] = (
                    "Remaining trial count isn't known yet: Telegram only reports it "
                    "after a transcription attempt. Up to "
                    f"{weekly_quota if weekly_quota is not None else 'a few'} messages/week, "
                    f"each up to {max_trial_duration_seconds if max_trial_duration_seconds is not None else 'a limited number of'} "
                    "seconds, are available until the first attempt this session."
                )

        return json.dumps(result, ensure_ascii=False, indent=2, default=json_serializer)
    except Exception as exc:
        return log_and_format_error("check_transcription_access", exc, account=account)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Transcribe Voice Messages",
        openWorldHint=True,
        readOnlyHint=True,
    )
)
@with_account(readonly=True)
@validate_id("chat_id")
async def transcribe_voice_messages(
    chat_id: Union[int, str],
    message_ids: List[int],
    retry_count: int = DEFAULT_RETRY_COUNT,
    retry_delay: float = DEFAULT_RETRY_DELAY,
    max_chunk_duration: int = DEFAULT_MAX_CHUNK_DURATION,
    max_chunk_messages: int = DEFAULT_MAX_CHUNK_MESSAGES,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    account: str = None,
) -> str:
    """Transcribe Telegram voice and audio messages with Telegram's native API."""
    try:
        if not isinstance(message_ids, list) or not message_ids:
            return "message_ids must be a non-empty list of Telegram message IDs."
        if len(message_ids) > 50:
            return "message_ids is limited to 50 items per call."

        retry_count = max(0, min(int(retry_count), 5))
        retry_delay = max(0.0, min(float(retry_delay), 10.0))
        max_chunk_duration = max(1.0, min(float(max_chunk_duration), 3600.0))
        max_chunk_messages = max(1, min(int(max_chunk_messages), 50))
        max_runtime_seconds = max(10.0, min(float(max_runtime_seconds), 110.0))
        request_timeout = max(5.0, min(float(request_timeout), 60.0))

        cl = get_client(account)
        await ensure_connected(cl)
        entity = await resolve_entity(chat_id, cl)

        is_premium = await _get_is_premium(cl)
        state = _account_state(cl)
        quota_remaining = None if is_premium else state["trial_remains_num"]

        started_at = time.monotonic()
        items = []
        entries = []
        for raw_message_id in message_ids:
            try:
                message_id = int(raw_message_id)
            except (TypeError, ValueError):
                items.append({"message_id": raw_message_id, "error": "message_id must be an integer."})
                continue

            msg = await cl.get_messages(entity, ids=message_id)
            if not msg:
                items.append({"message_id": message_id, "error": "Message not found."})
                continue

            media_kind = _message_media_kind(msg)
            if not media_kind:
                items.append(
                    {
                        "message_id": message_id,
                        "sender": get_sender_name(msg),
                        "date": getattr(msg, "date", None),
                        "error": "Message has no media to transcribe.",
                    }
                )
                continue
            if media_kind not in SUPPORTED_MEDIA_KINDS:
                items.append(
                    {
                        "message_id": message_id,
                        "sender": get_sender_name(msg),
                        "date": getattr(msg, "date", None),
                        "media": media_kind,
                        "error": _unsupported_media_message(media_kind),
                    }
                )
                continue

            entries.append(
                {
                    "message_id": message_id,
                    "msg": msg,
                    "duration": _message_media_duration(msg),
                    "media": media_kind,
                }
            )

        entries, skipped_quota_entries = _split_entries_by_quota(entries, quota_remaining)
        for entry in skipped_quota_entries:
            items.append(
                {
                    "message_id": entry["message_id"],
                    "sender": get_sender_name(entry["msg"]),
                    "date": getattr(entry["msg"], "date", None),
                    "media": entry["media"],
                    "error": _quota_exhausted_message(state["trial_remains_until_date"]),
                }
            )

        chunks = _chunk_message_entries(entries, max_chunk_duration, max_chunk_messages)
        processed_message_ids = []
        stopped_before = None
        quota_exhausted_mid_loop = False

        for chunk_index, chunk in enumerate(chunks):
            elapsed = time.monotonic() - started_at
            if elapsed + min(request_timeout, 10.0) >= max_runtime_seconds:
                stopped_before = chunk_index
                break

            for entry in chunk:
                elapsed = time.monotonic() - started_at
                if elapsed + min(request_timeout, 10.0) >= max_runtime_seconds:
                    stopped_before = chunk_index
                    break

                if quota_exhausted_mid_loop:
                    items.append(
                        {
                            "message_id": entry["message_id"],
                            "sender": get_sender_name(entry["msg"]),
                            "date": getattr(entry["msg"], "date", None),
                            "media": entry["media"],
                            "error": _quota_exhausted_message(state["trial_remains_until_date"]),
                        }
                    )
                    continue

                msg = entry["msg"]
                try:
                    payload = await _transcribe_one_message(
                        cl, entity, msg, retry_count, retry_delay, request_timeout
                    )
                except asyncio.TimeoutError:
                    payload = {
                        "message_id": msg.id,
                        "sender": get_sender_name(msg),
                        "date": getattr(msg, "date", None),
                        "media": _message_media_kind(msg),
                        "duration": _message_media_duration(msg),
                        "error": f"Telegram transcription request timed out after {request_timeout:.0f}s.",
                    }
                except PremiumAccountRequiredError:
                    # Telegram's authoritative answer: no free attempts left (or this
                    # message doesn't qualify). Record it so later calls in this
                    # session skip straight to the quota message instead of retrying.
                    state["trial_remains_num"] = 0
                    payload = {
                        "message_id": msg.id,
                        "sender": get_sender_name(msg),
                        "date": getattr(msg, "date", None),
                        "media": _message_media_kind(msg),
                        "duration": _message_media_duration(msg),
                        "error": _quota_exhausted_message(state["trial_remains_until_date"]),
                    }
                    quota_exhausted_mid_loop = True

                items.append(payload)
                processed_message_ids.append(msg.id)

            if stopped_before is not None:
                break

        remaining_message_ids = [
            entry["message_id"]
            for entry in entries
            if entry["message_id"] not in set(processed_message_ids)
        ]

        return json.dumps(
            {
                "chat_id": chat_id,
                "complete": not remaining_message_ids,
                "processed_message_ids": processed_message_ids,
                "remaining_message_ids": remaining_message_ids,
                "chunk_count": len(chunks),
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
                "messages": items,
                "next_call": (
                    {
                        "chat_id": chat_id,
                        "message_ids": remaining_message_ids,
                        "retry_count": retry_count,
                        "retry_delay": retry_delay,
                        "max_chunk_duration": max_chunk_duration,
                        "max_chunk_messages": max_chunk_messages,
                        "max_runtime_seconds": max_runtime_seconds,
                        "request_timeout": request_timeout,
                        "account": account,
                    }
                    if remaining_message_ids
                    else None
                ),
            },
            ensure_ascii=False,
            indent=2,
            default=json_serializer,
        )
    except Exception as exc:
        return log_and_format_error(
            "transcribe_voice_messages", exc, chat_id=chat_id, message_ids=message_ids
        )


async def _activate_reading_tools() -> str:
    """Put the reading tools back once the account is authorized.

    Returns ``"activated"`` if the host was told about the new tools, or
    ``"restart"`` if it has to be restarted to notice them.
    """
    for name, tool in _HIDDEN_TOOLS.items():
        mcp._tool_manager._tools.setdefault(name, tool)
    _HIDDEN_TOOLS.clear()

    # StringSession keeps no entity cache, so the first chat lookup would
    # otherwise pay for a full dialog fetch. Backgrounded for the same reason
    # the upstream runner backgrounds it: a flood wait here would hang the call.
    async def _warm_caches() -> None:
        try:
            await asyncio.gather(*(cl.get_dialogs() for cl in _runtime.clients.values()))
        except Exception as exc:
            print(f"Entity cache warm failed: {exc}", file=sys.stderr)

    asyncio.create_task(_warm_caches())

    try:
        await mcp.get_context().session.send_tool_list_changed()
        return "activated"
    except Exception as exc:
        print(f"telegram-mcp-ag: tools/list_changed failed: {exc}", file=sys.stderr)
        return "restart"


async def _run_login_server() -> None:
    """Serve only the login tools until the user has scanned the QR code."""
    try:
        for client in _runtime.clients.values():
            await client.connect()
        print(
            "No Telegram session yet: serving login tools only. "
            "Ask the assistant to connect Telegram.",
            file=sys.stderr,
        )
        await mcp.run_stdio_async()
    finally:
        await asyncio.gather(
            *(cl.disconnect() for cl in _runtime.clients.values()), return_exceptions=True
        )


def main() -> None:
    """Start the upstream STDIO server after registering this package's tools."""
    if not _LOGIN_MODE:
        _runner.main()
        return

    _runtime._configure_allowed_roots_from_cli(sys.argv[1:])
    _runtime._apply_exposed_tools_mode()

    # Everything except the login tools is withheld: without a session they
    # would only produce Telegram authorization errors, and this project's rule
    # is to check access up front rather than let the user hit an exception.
    for name in list(mcp._tool_manager._tools):
        if name not in _LOGIN_TOOLS:
            _HIDDEN_TOOLS[name] = mcp._tool_manager._tools.pop(name)

    nest_asyncio.apply()
    asyncio.run(_run_login_server())
