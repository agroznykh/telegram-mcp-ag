"""Telegram MCP launcher with safe, read-only voice transcription tools."""

from telegram_mcp import runner as _runner
from telegram_mcp.runtime import *


DEFAULT_MAX_CHUNK_DURATION = 180
DEFAULT_MAX_CHUNK_MESSAGES = 6
DEFAULT_MAX_RUNTIME_SECONDS = 100
DEFAULT_REQUEST_TIMEOUT = 30


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


def _transcription_payload(msg, result) -> dict:
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
    if trial_remains_num is not None:
        payload["trial_remains_num"] = trial_remains_num

    trial_remains_until_date = getattr(result, "trial_remains_until_date", None)
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
        last_payload = _transcription_payload(msg, result)
        last_payload["attempt"] = attempt + 1
        if not last_payload["pending"] or last_payload["text"]:
            break
        if attempt < retry_count:
            await asyncio.sleep(retry_delay)

    return last_payload


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
    retry_count: int = 2,
    retry_delay: float = 1.5,
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

            entries.append(
                {
                    "message_id": message_id,
                    "msg": msg,
                    "duration": _message_media_duration(msg),
                    "media": media_kind,
                }
            )

        chunks = _chunk_message_entries(entries, max_chunk_duration, max_chunk_messages)
        processed_message_ids = []
        stopped_before = None

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
    except telethon.errors.rpcerrorlist.PremiumAccountRequiredError:
        return "Telegram refused transcription: this session does not have access to voice transcription. Check that the session belongs to your Premium account."
    except Exception as exc:
        return log_and_format_error(
            "transcribe_voice_messages", exc, chat_id=chat_id, message_ids=message_ids
        )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Transcribe Voice Message",
        openWorldHint=True,
        readOnlyHint=True,
    )
)
@with_account(readonly=True)
@validate_id("chat_id")
async def transcribe_voice_message(
    chat_id: Union[int, str],
    message_id: int,
    retry_count: int = 2,
    retry_delay: float = 1.5,
    account: str = None,
) -> str:
    """Transcribe one Telegram voice, audio, or video-note message."""
    return await transcribe_voice_messages(
        chat_id=chat_id,
        message_ids=[message_id],
        retry_count=retry_count,
        retry_delay=retry_delay,
        account=account,
    )


def main() -> None:
    """Start the upstream STDIO server after registering this package's tools."""
    _runner.main()
