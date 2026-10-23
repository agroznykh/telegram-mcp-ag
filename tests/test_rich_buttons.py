import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession
from telethon.tl import functions, types


def _dummy_session_string() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(data=os.urandom(256))
    return session.save()


os.environ.setdefault("TELEGRAM_API_ID", "1")
os.environ.setdefault("TELEGRAM_API_HASH", "test-hash")
os.environ.setdefault("TELEGRAM_SESSION_STRING", _dummy_session_string())

from telegram_mcp_ag import rich_buttons  # noqa: E402


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _button(text, button_type=None, style=None):
    if button_type is None:
        button_type = types.InlineButtonTypeCallback(data=text.encode())
    return types.PageButton(text=types.TextPlain(text), type=button_type, style=style)


def _row(*buttons):
    return types.PageBlockButtonRow(buttons=list(buttons))


def _rich(*blocks, part=None):
    return types.RichMessage(blocks=list(blocks), photos=[], documents=[], part=part)


def _message(rich=None, message_id=7, buttons=None):
    return SimpleNamespace(
        id=message_id, date=None, rich_message=rich, buttons=buttons, reply_markup=None
    )


class FakeClient:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.requests = []
        self.answer = SimpleNamespace(message="ok", alert=None)
        self.full_rich = {}

    async def get_messages(self, entity, ids=None, limit=None):
        if ids is not None:
            return next((m for m in self.messages if m.id == ids), None)
        return self.messages[:limit]

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.GetRichMessageRequest):
            full = _message(self.full_rich[request.id], message_id=request.id)
            return SimpleNamespace(messages=[full])
        return self.answer


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()

    async def _connected(_):
        return None

    async def _entity(chat_id, _):
        return chat_id

    monkeypatch.setattr(rich_buttons, "get_client", lambda account=None: fake)
    monkeypatch.setattr(rich_buttons, "ensure_connected", _connected)
    monkeypatch.setattr(rich_buttons, "resolve_entity", _entity)
    return fake


def _run(coro):
    return asyncio.run(coro)


def _listed(raw):
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Collecting buttons
# ---------------------------------------------------------------------------


def test_row_of_three_buttons_is_collected_in_order():
    message = _message(_rich(_row(_button("1"), _button("2"), _button("3"))))

    assert [rich_buttons._typed_button_info(b).text for b in rich_buttons.collect_page_buttons(message)] == [
        "1",
        "2",
        "3",
    ]


def test_buttons_in_several_rows_keep_screen_order():
    message = _message(_rich(_row(_button("a"), _button("b")), _row(_button("c"))))

    assert [b.text for b in rich_buttons.collect_buttons(message)] == ["a", "b", "c"]


def test_buttons_inside_nested_blocks_are_found():
    details = types.PageBlockDetails(
        blocks=[_row(_button("deep"))], title=types.TextPlain("more")
    )
    message = _message(_rich(_row(_button("top")), details))

    assert [b.text for b in rich_buttons.collect_buttons(message)] == ["top", "deep"]


def test_style_is_reported_as_raw_flags():
    message = _message(
        _rich(
            _row(
                _button("plain"),
                _button("chosen", style=types.RichButtonStyle(bg_success=True)),
                _button("both", style=types.RichButtonStyle(bg_primary=True, link=True)),
            )
        )
    )

    plain, chosen, both = rich_buttons.collect_buttons(message)

    assert plain.style == []
    assert chosen.style == ["bg_success"]
    assert both.style == ["bg_primary", "link"]


def test_message_without_buttons_collects_nothing():
    assert rich_buttons.collect_buttons(_message(_rich())) == []
    assert rich_buttons.collect_buttons(_message(None)) == []


def test_rich_blocks_and_keyboard_share_one_numbering():
    keyboard = [[SimpleNamespace(text="kb", data=b"x", url=None)]]
    message = _message(_rich(_row(_button("rich"))), buttons=keyboard)

    assert [b.text for b in rich_buttons.collect_buttons(message)] == ["rich", "kb"]


def test_layer_229_inline_keyboard_buttons_are_read_by_type():
    keyboard = [
        [
            types.KeyboardInlineButton(
                text="Go",
                type=types.InlineButtonTypeCallback(data=b"g"),
                style=types.KeyboardButtonStyle(bg_success=True),
            ),
            types.KeyboardInlineButton(
                text="Site", type=types.InlineButtonTypeUrl(url="https://example.com")
            ),
        ]
    ]

    go, site = rich_buttons.collect_buttons(_message(None, buttons=keyboard))

    assert (go.kind, go.data, go.style, go.pressable) == ("callback", b"g", ["bg_success"], True)
    assert (site.kind, site.url, site.pressable) == ("url", "https://example.com", False)


def test_telethon_message_button_wrappers_keep_style_and_disabled():
    def wrapped(raw):
        # What Message.buttons yields: only text/data/url, the original is under `.button`.
        return SimpleNamespace(button=raw, text=raw.text, data=None, url=None)

    keyboard = [
        [
            wrapped(
                types.KeyboardInlineButton(
                    text="Go",
                    type=types.InlineButtonTypeCallback(data=b"g"),
                    style=types.KeyboardButtonStyle(bg_danger=True),
                )
            ),
            wrapped(types.KeyboardInlineButton(text="Off", type=types.InlineButtonTypeDisabled())),
        ]
    ]

    go, off = rich_buttons.collect_buttons(_message(None, buttons=keyboard))

    assert (go.kind, go.style, go.pressable) == ("callback", ["bg_danger"], True)
    assert (off.kind, off.pressable) == ("disabled", False)


def test_press_reaches_a_keyboard_button_below_rich_ones(client):
    keyboard = [[SimpleNamespace(text="kb", data=b"k", url=None)]]
    client.messages = [_message(_rich(_row(_button("rich"))), buttons=keyboard)]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=1))

    assert _pressed(client)[0].data == b"k"


# ---------------------------------------------------------------------------
# list_inline_buttons
# ---------------------------------------------------------------------------


def test_list_reports_index_text_kind_and_style(client):
    client.messages = [
        _message(
            _rich(
                _row(
                    _button("1"),
                    _button("2", style=types.RichButtonStyle(bg_success=True)),
                    _button("site", types.InlineButtonTypeUrl(url="https://example.com")),
                )
            )
        )
    ]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert [(r["index"], r["text"], r["kind"], r["has_callback"]) for r in result["results"]] == [
        (0, "1", "callback", True),
        (1, "2", "callback", True),
        (2, "site", "url", False),
    ]
    assert result["results"][1]["style"] == ["bg_success"]
    assert "style" not in result["results"][0]
    assert result["results"][2]["url"] == "https://example.com"
    assert result["message_id"] == 7


def test_list_without_buttons_keeps_the_old_answer(client):
    client.messages = [_message(_rich())]

    assert "does not contain inline buttons" in _run(
        rich_buttons.list_inline_buttons(chat_id=1, message_id=7)
    )


def test_list_without_any_message_keeps_the_old_answer(client):
    client.messages = [_message(None)]

    assert _run(rich_buttons.list_inline_buttons(chat_id=1)) == "No message with inline buttons found."


def test_list_without_message_id_skips_messages_without_buttons(client):
    client.messages = [_message(None, message_id=9), _message(_rich(_row(_button("1"))), message_id=8)]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert result["message_id"] == 8


def test_list_still_reads_an_ordinary_keyboard(client):
    client.messages = [_message(buttons=[[SimpleNamespace(text="kb", data=b"x", url=None)]])]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert result["results"][0]["text"] == "kb"
    assert result["results"][0]["has_callback"] is True


def test_list_asks_for_the_full_message_when_telegram_sent_a_part(client):
    client.messages = [_message(_rich(part=True))]
    client.full_rich[7] = _rich(_row(_button("late")))

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1, message_id=7)))

    assert result["results"][0]["text"] == "late"
    assert any(isinstance(r, functions.messages.GetRichMessageRequest) for r in client.requests)


def test_a_complete_message_is_not_refetched(client):
    client.messages = [_message(_rich(_row(_button("1"))))]

    _run(rich_buttons.list_inline_buttons(chat_id=1, message_id=7))

    assert client.requests == []


# ---------------------------------------------------------------------------
# press_inline_button
# ---------------------------------------------------------------------------


def _pressed(client):
    return [r for r in client.requests if isinstance(r, functions.messages.GetBotCallbackAnswerRequest)]


def test_press_by_index_sends_the_callback_data(client):
    client.messages = [_message(_rich(_row(_button("1"), _button("2"))), message_id=42)]

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=42, button_index=1))

    (request,) = _pressed(client)
    assert (request.peer, request.msg_id, request.data) == (5, 42, b"2")
    assert _listed(result)["response"] == "ok"


def test_press_by_text_ignores_case_and_spaces(client):
    client.messages = [_message(_rich(_row(_button("Yes"), _button("No"))))]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="  no "))

    assert _pressed(client)[0].data == b"No"


def test_press_without_message_id_takes_the_latest_message_with_buttons(client):
    client.messages = [_message(None, message_id=9), _message(_rich(_row(_button("1"))), message_id=8)]

    _run(rich_buttons.press_inline_button(chat_id=5, button_index=0))

    assert _pressed(client)[0].msg_id == 8


def test_press_reports_an_alert(client):
    client.messages = [_message(_rich(_row(_button("1"))))]
    client.answer = SimpleNamespace(message=None, alert=True)

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0))

    assert "alert" in _listed(result)["response"]


def test_press_with_nothing_chosen_asks_for_a_choice(client):
    assert "button_text or button_index" in _run(rich_buttons.press_inline_button(chat_id=5))


def test_press_out_of_range_names_the_valid_indices(client):
    client.messages = [_message(_rich(_row(_button("1"), _button("2"))))]

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=5))

    assert "0-1" in result
    assert _pressed(client) == []


def test_press_unknown_text_lists_the_available_buttons(client):
    client.messages = [_message(_rich(_row(_button("1"), _button("2"))))]

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="zzz"))

    assert "[0] 1" in result and "[1] 2" in result
    assert _pressed(client) == []


@pytest.mark.parametrize(
    "button_type, expected",
    [
        (types.InlineButtonTypeCallback(data=b"x", requires_password=True), "password"),
        (types.InlineButtonTypeDisabled(), "disabled"),
        (types.InlineButtonTypeUrl(url="https://example.com"), "https://example.com"),
        (types.InlineButtonTypeCopy(copy_text="abc"), "'copy'"),
    ],
)
def test_press_refuses_what_cannot_be_pressed(client, button_type, expected):
    client.messages = [_message(_rich(_row(_button("b", button_type))))]

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0))

    assert expected in result
    assert _pressed(client) == []


def test_press_still_works_on_an_ordinary_keyboard(client):
    client.messages = [_message(buttons=[[SimpleNamespace(text="kb", data=b"k", url=None)]])]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="kb"))

    assert _pressed(client)[0].data == b"k"
