# Кнопки rich-сообщений: справка

Статус: **сделано и проверено на живом боте** (версия 0.3.2, 23.10.2026).
Код — `src/telegram_mcp_ag/rich_buttons.py`, тесты — `tests/test_rich_buttons.py`.
Версии: Telethon 1.45.0 (слой 229), апстрим `chigwell/telegram-mcp` по SHA из `pyproject.toml`.

## Что было не так

Инструменты апстрима `list_inline_buttons` и `press_inline_button` смотрели только
`message.buttons` и `reply_markup`. У сообщения в формате Rich Message (Bot API 10.1+)
обычного `reply_markup` нет: содержимое лежит в `message.rich_message.blocks` как блоки
в стиле Instant View, а кнопки — тоже блоки:

```
message.rich_message              # RichMessage(blocks, photos, documents, rtl, part)
  └─ blocks[]                     # PageBlock*
       └─ PageBlockButtonRow(buttons[], align_left/center/right)
            └─ PageButton(text: RichText, type: InlineButtonType*, style: RichButtonStyle | None)
                 └─ type = InlineButtonTypeCallback(data: bytes, requires_password: bool | None)
```

Ответ был «Message … does not contain inline buttons», хотя в клиенте кнопки есть.

## Что сделано

Оба инструмента переопределены в обёртке (апстрим не тронут, инвариант «закреплён по SHA»).
Имена и параметры прежние.

- Кнопки собираются рекурсивно из `rich_message.blocks` в экранном порядке, затем идут
  кнопки обычной клавиатуры. Нумерация сквозная, индекс из списка подходит для нажатия.
- `list_inline_buttons` отдаёт: `index`, `text`, `kind` (`callback`, `url`, `disabled`,
  `web_view`, `copy`…), `has_callback`, а также `style`, `url`, `requires_password`, когда они есть.
- `style` — сырые флаги Telegram (`bg_primary`, `bg_success`, `bg_danger`, `link`).
  Сервер их **не интерпретирует**: что значит «выбрано», решает вызывающий.
- `press_inline_button` для callback-кнопки шлёт `messages.getBotCallbackAnswer` с
  `PageButton.type.data`; ответ бота возвращается как раньше.
- Отказ без запроса в Telegram: `requires_password` (пароль 2FA через чат не берём),
  `disabled`, URL и клиентские типы (`copy`, `web_view`, `switch_inline` и т. п.).
- Если у rich-сообщения `part == True`, запрашивается полная версия
  `messages.getRichMessage` (в TDLib — `getFullRichMessage`).
- Права не менялись: `list_inline_buttons` есть в `read-only`, `press_inline_button` — нет.
- Подписи кнопок в текст истории (`get_messages` и т. п.) **не добавлялись** — сознательно:
  потребовался бы патч `rich_message_text` апстрима, история стала бы длиннее, а кнопки
  в старых сообщениях уже неактуальны.

## Что выяснилось при проверке

- **Нажатие работает** по схеме: `getBotCallbackAnswer` с `data` кнопки, бот получает
  обычный callback и редактирует сообщение на месте. Premium не нужен.
- **Многовыбор:** выбранные пункты отличаются только `style` (`bg_success` против
  `bg_primary`); недоступное «Далее» приходит как `kind: disabled`, после выбора — `callback`.
- **Inline-клавиатуры в слое 229 приходят как `KeyboardInlineButton(text, type, style)`**,
  то есть в том же виде, что и rich-кнопки, а не как `KeyboardButtonCallback` с `data`.
  Код читает оба формата через общий `type`; без этого такие кнопки выглядели бы
  как «не callback».
- **Rich-кнопки и inline-клавиатура одного сообщения** работают вместе: в списке сначала
  rich (индексы с 0), затем клавиатура.
- **Telegram обрезает rich-сообщение примерно на 65 блоках** (в тесте 300 абзацев доехало
  как 66 блоков, `part == False`). Кнопки, стоящие после обрезки, не доходят даже до клиента.
  Реальные экраны (десятки блоков) под это не попадают.

## Что такое `part`

Флаг `part` в `richMessage` — зеркало поля `is_full` из TDLib («True, если объект содержит
полное сообщение; иначе для получения полного нужно использовать `getFullRichMessage`»).
`part == True` — Telegram прислал сокращённую версию. Когда именно это случается, в схеме
и TDLib не сказано; на практике не наблюдалось, путь с `GetRichMessageRequest` проверен
только тестом.

## Не проверено

- Реальный случай `part == True`.
- Нажатие кнопок с `requires_password` на живом сообщении (отказ покрыт тестом).

## Тесты (pytest, без сети)

Ряды и вложенные блоки, стили, общая нумерация rich и клавиатуры, клавиатура слоя 229,
нажатие по индексу и по подписи, отказы (`requires_password`, `disabled`, URL, `copy`),
прежние ответы «нет кнопок», запрос полной версии при `part`.

## Источники

- Схема MTProto: `td/generate/scheme/telegram_api.tl` и `td_api.tl` в github.com/tdlib/td
  (`pageBlockButtonRow`, `pageButton`, `richMessage`, `is_full`).
- Bot API: core.telegram.org/bots/api (`Message.rich_message`, `RichMessageButton`),
  журнал изменений core.telegram.org/bots/api-changelog (10.1 — Rich Messages, 10.3 — `RichMessageButton`).
- Telethon 1.45: `telethon/tl/types` (`PageBlockButtonRow`, `PageButton`,
  `KeyboardInlineButton`, `InlineButtonType*`).
- На публичных страницах MTProto про rich-кнопки ничего нет; вывод опирается на схему,
  код TDLib и живую проверку.
