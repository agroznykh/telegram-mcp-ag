import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from telethon import errors
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


def _message(rich=None, message_id=7, markup=None):
    return SimpleNamespace(id=message_id, date=None, rich_message=rich, reply_markup=markup)


def _inline_button(text, button_type=None, style=None):
    if button_type is None:
        button_type = types.InlineButtonTypeCallback(data=text.encode())
    return types.KeyboardInlineButton(text=text, type=button_type, style=style)


def _inline(*rows):
    """An inline keyboard; each argument is one row of buttons."""
    return types.ReplyInlineMarkup(rows=[types.KeyboardInlineButtonRow(buttons=list(r)) for r in rows])


def _reply_button(text, button_type=None):
    return types.KeyboardButton(text=text, type=button_type or types.ButtonTypeDefault())


def _reply(*rows, **flags):
    """A reply keyboard (the bot's menu in place of the input field)."""
    return types.ReplyKeyboardMarkup(
        rows=[types.KeyboardButtonRow(buttons=list(r)) for r in rows], **flags
    )


class FakeClient:
    def __init__(self, *messages):
        self.messages = list(messages)
        self.requests = []
        self.answer = SimpleNamespace(message="ok", alert=None)
        self.full_rich = {}
        self.raises = None

    async def get_messages(self, entity, ids=None, limit=None):
        if ids is not None:
            return next((m for m in self.messages if m.id == ids), None)
        return self.messages[:limit]

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, functions.messages.GetRichMessageRequest):
            full = _message(self.full_rich[request.id], message_id=request.id)
            return SimpleNamespace(messages=[full])
        if self.raises is not None:
            raise self.raises
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

    assert [b.text for b in rich_buttons.collect_buttons(message)] == ["1", "2", "3"]


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
    message = _message(_rich(_row(_button("rich"))), markup=_inline([_inline_button("kb")]))

    assert [b.text for b in rich_buttons.collect_buttons(message)] == ["rich", "kb"]


def test_inline_keyboard_buttons_are_read_by_type():
    markup = _inline(
        [
            _inline_button(
                "Go",
                types.InlineButtonTypeCallback(data=b"g"),
                types.KeyboardButtonStyle(bg_success=True),
            ),
            _inline_button("Site", types.InlineButtonTypeUrl(url="https://example.com")),
        ]
    )

    go, site = rich_buttons.collect_buttons(_message(None, markup=markup))

    assert (go.kind, go.data, go.style, go.pressable) == ("callback", b"g", ["bg_success"], True)
    assert (site.kind, site.url, site.pressable) == ("url", "https://example.com", False)


def test_keyboard_is_read_from_reply_markup_not_from_telethon_wrappers():
    # Message.buttons wraps buttons and hides their type and style (and can be missing
    # on a message fetched by id): only reply_markup is read, wrappers are ignored.
    markup = _inline(
        [
            _inline_button(
                "Go",
                types.InlineButtonTypeCallback(data=b"g"),
                types.KeyboardButtonStyle(bg_danger=True),
            ),
            _inline_button("Off", types.InlineButtonTypeDisabled()),
        ]
    )
    message = _message(None, markup=markup)
    message.buttons = [[SimpleNamespace(text="wrapper", data=b"w", url=None)]]

    go, off = rich_buttons.collect_buttons(message)

    assert (go.kind, go.style, go.pressable) == ("callback", ["bg_danger"], True)
    assert (off.kind, off.pressable) == ("disabled", False)


def test_buttons_of_an_older_layer_without_type_are_still_read():
    legacy = [
        SimpleNamespace(text="cb", data=b"x", url=None),
        SimpleNamespace(text="link", data=None, url="https://example.com"),
        SimpleNamespace(text="odd", data=None, url=None),
    ]
    markup = types.ReplyInlineMarkup(rows=[SimpleNamespace(buttons=legacy)])

    cb, link, odd = rich_buttons.collect_buttons(_message(None, markup=markup))

    assert [(b.kind, b.pressable) for b in (cb, link, odd)] == [
        ("callback", True),
        ("url", False),
        ("unknown", False),
    ]


def test_markup_that_only_hides_the_keyboard_has_no_buttons():
    for markup in (types.ReplyKeyboardHide(), types.ReplyKeyboardForceReply()):
        assert rich_buttons.collect_buttons(_message(None, markup=markup)) == []


def test_press_reaches_a_keyboard_button_below_rich_ones(client):
    client.messages = [_message(_rich(_row(_button("rich"))), markup=_inline([_inline_button("k")]))]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=1))

    assert _pressed(client)[0].data == b"k"


def _positions(message):
    return [(b.text, b.row, b.col) for b in rich_buttons.collect_buttons(message)]


def test_rich_rows_and_columns_are_numbered_from_zero():
    message = _message(_rich(_row(_button("a"), _button("b")), _row(_button("c"))))

    assert _positions(message) == [("a", 0, 0), ("b", 0, 1), ("c", 1, 0)]


def test_a_rich_button_outside_a_row_is_a_row_of_its_own():
    message = _message(_rich(_button("alone"), _row(_button("a"), _button("b"))))

    assert _positions(message) == [("alone", 0, 0), ("a", 1, 0), ("b", 1, 1)]


def test_keyboard_rows_and_columns_are_numbered_from_zero():
    message = _message(None, markup=_inline([_inline_button("1"), _inline_button("2")], [_inline_button("3")]))

    assert _positions(message) == [("1", 0, 0), ("2", 0, 1), ("3", 1, 0)]


def test_rows_continue_across_rich_blocks_and_keyboard():
    message = _message(
        _rich(_row(_button("r1"), _button("r2")), _row(_button("r3"))),
        markup=_inline([_inline_button("k1")], [_inline_button("k2"), _inline_button("k3")]),
    )

    assert _positions(message) == [
        ("r1", 0, 0),
        ("r2", 0, 1),
        ("r3", 1, 0),
        ("k1", 2, 0),
        ("k2", 3, 0),
        ("k3", 3, 1),
    ]


def _text_button(text):
    return types.TextButton(text=types.TextPlain(text), type=types.InlineButtonTypeCallback(data=text.encode()))


def _cell(text):
    return types.PageTableCell(text=text)


def _board(*rows):
    """A table whose cells are plain text or text buttons -- like a chess board."""
    return types.PageBlockTable(
        title=types.TextEmpty(),
        rows=[types.PageTableRow(cells=[_cell(c) for c in row]) for row in rows],
    )


def test_text_buttons_in_a_table_make_a_row_per_table_row():
    board = _board(
        [types.TextPlain("a"), types.TextPlain("b")],  # header: no buttons, takes no row
        [types.TextPlain("1"), _text_button("a1"), _text_button("b1")],
        [types.TextPlain("2"), _text_button("a2"), _text_button("b2")],
    )
    message = _message(_rich(board, _row(_button("Undo"))))

    assert _positions(message) == [
        ("a1", 0, 0),
        ("b1", 0, 1),
        ("a2", 1, 0),
        ("b2", 1, 1),
        ("Undo", 2, 0),
    ]


def test_text_buttons_in_a_paragraph_make_one_row():
    paragraph = types.PageBlockParagraph(
        text=types.TextConcat(texts=[types.TextPlain("Pick: "), _text_button("x"), _text_button("y")])
    )
    message = _message(_rich(_row(_button("top")), paragraph))

    assert _positions(message) == [("top", 0, 0), ("x", 1, 0), ("y", 1, 1)]


def test_a_text_button_is_found_and_pressed(client):
    client.messages = [_message(_rich(_board([_text_button("e2"), _text_button("e4")])))]

    listed = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))
    _run(rich_buttons.press_inline_button(chat_id=5, button_text="e4"))

    assert [b["text"] for b in listed["results"]] == ["e2", "e4"]
    assert _pressed(client)[0].data == b"e4"


# ---------------------------------------------------------------------------
# list_inline_buttons
# ---------------------------------------------------------------------------


def test_list_reports_row_and_col(client):
    client.messages = [_message(_rich(_row(_button("a"), _button("b")), _row(_button("c"))))]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))["results"]

    assert [(r["index"], r["row"], r["col"]) for r in result] == [(0, 0, 0), (1, 0, 1), (2, 1, 0)]


def test_list_of_a_reply_keyboard_reports_placeholder_and_single_use(client):
    markup = _reply(
        [_reply_button("Menu"), _reply_button("Call me", types.ButtonTypeRequestPhone())],
        placeholder="Pick one",
        single_use=True,
    )
    client.messages = [_message(markup=markup)]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert result["keyboard"] == "reply"
    assert result["placeholder"] == "Pick one"
    assert result["single_use"] is True
    assert [(r["text"], r["kind"], r["has_callback"]) for r in result["results"]] == [
        ("Menu", "text", False),
        ("Call me", "request_phone", False),
    ]


def test_list_of_a_reply_keyboard_without_placeholder_leaves_it_out(client):
    client.messages = [_message(markup=_reply([_reply_button("Menu")]))]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert "placeholder" not in result
    assert result["single_use"] is False


def test_list_has_no_keyboard_field_for_rich_buttons_only(client):
    client.messages = [_message(_rich(_row(_button("1"))))]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert "keyboard" not in result
    assert "placeholder" not in result


def test_list_sanitizes_the_placeholder(client):
    client.messages = [_message(markup=_reply([_reply_button("Menu")], placeholder="x" * 1000))]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert len(result["placeholder"]) <= 300


@pytest.mark.parametrize(
    "button, kind",
    [
        (_reply_button("t"), "text"),
        (_reply_button("p", types.ButtonTypeRequestPhone()), "request_phone"),
        (_reply_button("l", types.ButtonTypeRequestGeoLocation()), "request_location"),
        (_reply_button("q", types.ButtonTypeRequestPoll()), "request_poll"),
        (
            _reply_button(
                "u",
                types.ButtonTypeRequestPeer(
                    button_id=1, peer_type=types.RequestPeerTypeUser(), max_quantity=1
                ),
            ),
            "request_peer",
        ),
        (_reply_button("w", types.ButtonTypeSimpleWebView(url="https://example.com")), "web_app"),
    ],
)
def test_reply_keyboard_kinds(button, kind):
    (info,) = rich_buttons.collect_buttons(_message(None, markup=_reply([button])))

    assert info.kind == kind


@pytest.mark.parametrize(
    "button_type, kind",
    [
        (types.InlineButtonTypeCallback(data=b"x"), "callback"),
        (types.InlineButtonTypeUrl(url="https://example.com"), "url"),
        (types.InlineButtonTypeSwitchInline(query="q"), "switch_inline"),
        (types.InlineButtonTypeGame(), "game"),
        (types.InlineButtonTypeBuy(), "buy"),
        (types.InlineButtonTypeUrlAuth(url="https://example.com", button_id=1), "url_auth"),
        (types.InlineButtonTypeWebView(url="https://example.com"), "web_view"),
        (types.InlineButtonTypeCopy(copy_text="c"), "copy"),
        (types.InlineButtonTypeUserProfile(user_id=5), "user_profile"),
        (types.InlineButtonTypeDisabled(), "disabled"),
    ],
)
def test_inline_kinds_are_the_same_for_rich_and_keyboard_buttons(button_type, kind):
    rich = rich_buttons.collect_buttons(_message(_rich(_row(_button("b", button_type)))))
    keyboard = rich_buttons.collect_buttons(
        _message(None, markup=_inline([_inline_button("b", button_type)]))
    )

    assert rich[0].kind == keyboard[0].kind == kind


def _only_record(message, client):
    client.messages = [message]
    return _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))["results"][0]


class InlineButtonTypeFromTheFuture(types.InlineButtonTypeBuy):
    """A type a newer layer may add and this code does not know."""


def test_unknown_type_is_named_and_not_pressable(client):
    message = _message(None, markup=_inline([_inline_button("x", InlineButtonTypeFromTheFuture())]))
    record = _only_record(message, client)

    assert record["kind"] == "unknown"
    assert record["type"] == "InlineButtonTypeFromTheFuture"
    assert "acts on the client side" in _run(
        rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0)
    )


def test_switch_inline_fields(client):
    button_type = types.InlineButtonTypeSwitchInline(query="search me", same_peer=True)

    record = _only_record(_message(_rich(_row(_button("s", button_type)))), client)

    assert (record["query"], record["same_peer"]) == ("search me", True)


def test_copy_user_profile_and_quiz_fields(client):
    client.messages = [
        _message(
            None,
            markup=_inline(
                [
                    _inline_button("c", types.InlineButtonTypeCopy(copy_text="PROMO")),
                    _inline_button("u", types.InlineButtonTypeUserProfile(user_id=77)),
                ]
            ),
        )
    ]

    copy_, profile = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))["results"]

    assert copy_["copy_text"] == "PROMO"
    assert profile["user_id"] == 77


def test_quiz_is_reported_only_when_set(client):
    unset = _only_record(
        _message(None, markup=_reply([_reply_button("q", types.ButtonTypeRequestPoll())])), client
    )
    quiz = _only_record(
        _message(None, markup=_reply([_reply_button("q", types.ButtonTypeRequestPoll(quiz=True))])),
        client,
    )

    assert "quiz" not in unset
    assert quiz["quiz"] is True


def test_url_is_reported_for_every_url_kind(client):
    client.messages = [
        _message(
            None,
            markup=_inline(
                [
                    _inline_button("a", types.InlineButtonTypeUrl(url="https://a.example")),
                    _inline_button("b", types.InlineButtonTypeUrlAuth(url="https://b.example", button_id=1)),
                    _inline_button("c", types.InlineButtonTypeWebView(url="https://c.example")),
                ]
            ),
        )
    ]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))["results"]

    assert [r["url"] for r in result] == ["https://a.example", "https://b.example", "https://c.example"]


def test_icon_emoji_id_comes_from_keyboard_button_style(client):
    record = _only_record(
        _message(
            None,
            markup=_inline(
                [_inline_button("i", style=types.KeyboardButtonStyle(icon=5368324170671202286))]
            ),
        ),
        client,
    )

    assert record["icon_emoji_id"] == "5368324170671202286"


def test_rich_buttons_have_no_icon(client):
    record = _only_record(_message(_rich(_row(_button("1", style=types.RichButtonStyle(link=True))))), client)

    assert "icon_emoji_id" not in record
    assert record["style"] == ["link"]


def test_empty_query_and_copy_text_stay_empty(client):
    client.messages = [
        _message(
            None,
            markup=_inline(
                [
                    _inline_button("s", types.InlineButtonTypeSwitchInline(query="")),
                    _inline_button("c", types.InlineButtonTypeCopy(copy_text="")),
                ]
            ),
        )
    ]

    switch, copy_ = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))["results"]
    refusal = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=1))

    assert switch["query"] == ""
    assert copy_["copy_text"] == ""
    assert "[empty]" not in refusal


def test_urls_are_cleaned_and_cut(client):
    long_url = "https://example.com/" + "a" * 5000
    client.messages = [
        _message(None, markup=_inline([_inline_button("u", types.InlineButtonTypeUrl(url=long_url))]))
    ]
    client.answer = SimpleNamespace(message=None, alert=None, url=long_url)

    listed = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))["results"][0]
    refusal = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0))

    assert len(listed["url"]) < 2200
    assert len(refusal) < 2400

    client.messages = [_message(None, markup=_inline([_inline_button("cb")]))]
    pressed = _listed(_run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0)))
    assert len(pressed["url"]) < 2200


def test_button_text_as_shown_by_the_tools_is_accepted(client):
    zero_width = "Buy​ now"
    long_label = "L" * 100
    client.messages = [
        _message(_rich(_row(_button(zero_width), _button(long_label)))),
    ]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="Buy now"))
    shown = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))["results"][1]["text"]
    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text=shown))

    assert [r.data for r in _pressed(client)] == [zero_width.encode(), long_label.encode()]


@pytest.mark.parametrize(
    "label",
    [
        "\U0001f504 Undo",  # no-break space: the listing shows a plain space
        "\U0001f504️ Undo",  # emoji variation selector: invisible in the listing
        "\U0001f504  Undo",  # doubled space
    ],
)
def test_press_by_text_ignores_characters_the_listing_does_not_show(client, label):
    client.messages = [_message(_rich(_row(_button("Flip"), _button(label))))]

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="\U0001f504 undo"))

    assert "Button not found" not in result
    assert _pressed(client)[0].data == label.encode()


def test_press_by_text_still_tells_different_labels_apart(client):
    client.messages = [_message(_rich(_row(_button("\U0001f504 Undo"), _button("\U0001f504 Redo"))))]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="\U0001f504 redo"))

    assert _pressed(client)[0].data == "\U0001f504 Redo".encode()


def test_old_layer_callback_with_password_is_not_pressable():
    legacy = SimpleNamespace(text="p", data=b"x", url=None, requires_password=True)
    markup = types.ReplyInlineMarkup(rows=[SimpleNamespace(buttons=[legacy])])

    (info,) = rich_buttons.collect_buttons(_message(None, markup=markup))

    assert info.requires_password is True
    assert info.pressable is False


def test_empty_keyboard_row_takes_no_row_number():
    markup = _inline([_inline_button("a")], [], [_inline_button("b")])

    assert _positions(_message(None, markup=markup)) == [("a", 0, 0), ("b", 1, 0)]


def test_errors_go_through_the_function_server_py_patches(client, monkeypatch):
    client.messages = [_message(_rich(_row(_button("1"))))]
    client.raises = RuntimeError("boom")
    monkeypatch.setattr(rich_buttons._runtime, "log_and_format_error", lambda *a, **k: "PATCHED")

    assert _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0)) == "PATCHED"


def test_password_callback_is_flagged_and_not_pressable(client):
    record = _only_record(
        _message(
            _rich(_row(_button("p", types.InlineButtonTypeCallback(data=b"x", requires_password=True))))
        ),
        client,
    )

    assert record["requires_password"] is True
    assert record["has_callback"] is False


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
    client.messages = [_message(markup=_inline([_inline_button("kb")]))]

    result = _listed(_run(rich_buttons.list_inline_buttons(chat_id=1)))

    assert result["results"][0]["text"] == "kb"
    assert result["results"][0]["has_callback"] is True
    assert result["keyboard"] == "inline"


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

    result = _listed(_run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0)))

    assert result["alert"] is True
    assert result["response"] == "Button pressed successfully."


def test_press_with_text_and_alert_says_so(client):
    client.messages = [_message(_rich(_row(_button("1"))))]
    client.answer = SimpleNamespace(message="Sold out", alert=True)

    result = _listed(_run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0)))

    assert result["response"] == "Sold out Telegram displayed an alert to the user."
    assert result["alert"] is True


def test_press_without_alert_reports_alert_false_and_the_bots_url(client):
    client.messages = [_message(_rich(_row(_button("1"))))]
    client.answer = SimpleNamespace(message=None, alert=None, url="https://t.me/bot?start=x")

    result = _listed(_run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0)))

    assert result["alert"] is False
    assert result["url"] == "https://t.me/bot?start=x"


def test_press_without_a_bot_url_has_no_url_field(client):
    client.messages = [_message(_rich(_row(_button("1"))))]

    result = _listed(_run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0)))

    assert "url" not in result


def test_press_game_button_sends_the_game_flag(client):
    client.messages = [_message(markup=_inline([_inline_button("Play", types.InlineButtonTypeGame())]))]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0))

    (request,) = _pressed(client)
    assert request.game is True
    assert not request.data


@pytest.mark.parametrize(
    "error, expected",
    [
        (errors.BotResponseTimeoutError(None), "did not answer the button press in time"),
        (errors.DataInvalidError(None), "keyboard has probably changed"),
        (errors.MessageIdInvalidError(None), "Message 7 is no longer in this chat."),
    ],
)
def test_press_errors_come_back_as_text(client, error, expected):
    client.messages = [_message(_rich(_row(_button("1"))))]
    client.raises = error

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0))

    assert expected in result


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
        (
            types.InlineButtonTypeUrl(url="https://example.com/u"),
            "opens a URL instead of sending a callback: https://example.com/u",
        ),
        (
            types.InlineButtonTypeUrlAuth(url="https://example.com/a", button_id=1),
            "opens a URL instead of sending a callback: https://example.com/a",
        ),
        (
            types.InlineButtonTypeWebView(url="https://example.com/w"),
            "opens a URL instead of sending a callback: https://example.com/w",
        ),
        (
            types.ButtonTypeSimpleWebView(url="https://example.com/s"),
            "opens a URL instead of sending a callback: https://example.com/s",
        ),
        (
            types.InlineButtonTypeCopy(copy_text="PROMO10"),
            "copies text instead of sending a callback: PROMO10",
        ),
        (
            types.ButtonTypeDefault(),
            "Use send_message with the button text instead (not available in read-only mode)",
        ),
        (types.ButtonTypeRequestPhone(), "share your phone number with the bot"),
        (types.ButtonTypeRequestGeoLocation(), "share your location with the bot"),
        (types.ButtonTypeRequestPoll(), "acts on the client side"),
        (types.InlineButtonTypeSwitchInline(query="q"), "acts on the client side"),
        (types.InlineButtonTypeBuy(), "acts on the client side"),
        (types.InlineButtonTypeUserProfile(user_id=5), "acts on the client side"),
    ],
)
def test_press_refuses_what_cannot_be_pressed(client, button_type, expected):
    # Reply-keyboard types come in a ReplyKeyboardMarkup, inline ones in a rich message,
    # as Telegram sends them: the schema does not allow a reply type in a PageButton.
    if type(button_type).__name__.startswith("ButtonType"):
        client.messages = [_message(markup=_reply([_reply_button("b", button_type)]))]
    else:
        client.messages = [_message(_rich(_row(_button("b", button_type))))]

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_index=0))

    assert expected in result
    assert _pressed(client) == []


def test_press_a_reply_keyboard_button_is_refused_with_the_send_message_hint(client):
    client.messages = [_message(markup=_reply([_reply_button("📋 Menu")]))]

    result = _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="📋 menu"))

    assert "reply keyboard" in result
    assert _pressed(client) == []


def test_press_still_works_on_an_ordinary_keyboard(client):
    client.messages = [_message(markup=_inline([_inline_button("kb", types.InlineButtonTypeCallback(b"k"))]))]

    _run(rich_buttons.press_inline_button(chat_id=5, message_id=7, button_text="kb"))

    assert _pressed(client)[0].data == b"k"
