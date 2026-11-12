# Bot API 10.4: HTML-виджеты и другие новинки

**Дата сбора информации:** 2026-11-10 (релиз Bot API 10.4 — 2026-11-09).
**Источники:** пост https://t.me/BotNews/122 и страницы core.telegram.org, перечисленные ниже.
Документация меняется быстро — перед работой перепроверять.

## Где это доступно (на 2026-11-10)

| Что | Продакшн | Тестовый сервер |
|---|---|---|
| Виджет в личном чате с ботом (с запросами и кошельком) | да | да |
| Виджеты в группах | нет | да |
| Виджеты в inline-режиме | нет | да |
| Пересылка сообщений с виджетом | нет | да |
| Виджеты в каналах | нет (и на тесте нельзя, только пересылкой) | — |

- **Версия приложения Telegram, с которой виджеты работают, в документации не указана.**
  Сказано только: старые приложения показывают сообщение как «неподдерживаемое медиа» с подписью.
  Кошелёк (`TelegramWidget.wallet`) может быть недоступен в старом приложении или в стране без кошелька.
- Профиль виджетов: `1` (`TelegramWidget.profile`). Следующие профили только добавляют возможности.
- Тестовый сервер: на нём у любого бота доступен полный набор.

## Что нового в 10.4

1. **HTML-виджеты** — интерактивный блок внутри пузыря сообщения (главное).
2. **Кошелёк TON** — подпись и платёж из виджета, карточка кошелька пользователя, служебные сообщения о переводах.
3. **Эфемерные ответы на нажатие кнопки** — виден только нажавшему, в любом чате, кроме каналов.
4. **Цитаты из rich-сообщений** (`quote_rich_texts` в `ReplyParameters`).
5. **Подарки**: `getUniqueGift` (по имени), `deleteBusinessAccountGift`.
6. **Вторичные юзернеймы** ботов (`agent`, `ai`, `app`, `game`, `gay`) — только Premium, до 2 ботов.
7. Мелочи: темы экрана «поделиться», новые типы сущностей `ton_address` и `bank_card_number`, `PaidMessagesWaived`, формат ссылок на топики в личных чатах.

## Как устроен виджет

- Один HTML-документ с inline CSS и JS. **До 32 КБ HTML и до 4 КБ после сжатия zlib -9.**
- JS выполняется в песочнице QuickJS на устройстве пользователя. **Сети нет вообще** (`fetch`, WebSocket — ошибка).
  Внешние скрипты, шрифты, картинки не грузятся, библиотек нет.
- **Код виджета неизменен** после отправки. Меняется только **состояние**, и пишет его только бот:
  - `chat_data` — состояние одного сообщения (до 1024 байт);
  - `public_data` — общее состояние везде, где виджет показан (до 1024 байт).
- **Статичный** виджет хранит всё в сообщении. **Общий** (`is_shared`) — один объект на сервере, его показывают много сообщений
  (как опрос), у них одно общее состояние. Идентификатор — `widget.id`.
- Связь с ботом только через `TelegramWidget.request(data)` (1–64 байта). Работает как callback-кнопка.
- **Ввода текста нет**: ни полей, ни клавиатуры. Есть только нажатия, pointer-события и горизонтальные жесты.
  Вертикальные жесты всегда прокручивают чат.

## Как бот получает действия пользователя

1. Виджет вызывает `TelegramWidget.request('vote:2')` внутри обработчика нажатия.
2. Боту приходит update `widget_query` (из пересланного общего виджета — `shared_widget_query`).
   В нём: `id`, `from` (проверен Telegram, подделать нельзя), `data`, чат и `message_id`, `widget_id`.
   Если бот задаёт `allowed_updates`, нужно включить оба.
3. Бот **в течение 15 секунд** вызывает `answerWidgetQuery` (ответ до 1024 байт, получит только спросивший виджет).
4. Состояние меняется отдельно: `editMessageWidgetData` (по чату и сообщению) или `editSharedWidgetData` (по `widget_id`).
   Виджет получает события `chatDataChanged` / `publicDataChanged` и перерисовывается.

Ограничения: 5 запросов за 5 секунд на пользователя и бота; один запрос одновременно; запрос только по нажатию
(в фоне — только в личном чате с ботом, раз при появлении и дальше не чаще раза в 5 секунд).
Правка состояния считается отправленным сообщением в лимитах бота; у общего виджета — не чаще 1 правки в секунду.
Запрос не содержит состояния виджета — бот хранит его у себя и проверяет `data` как недоверенный ввод.

## Что ещё поддерживает виджет

- **HTML:** текст, списки, таблицы, `details`, `button`, `canvas`, `img` только из `data:` (PNG/JPEG/GIF/WebP), inline SVG. Нет `input`, `select`, `textarea`, `iframe`, `video`, `audio`.
- **CSS:** flex, grid, градиенты, 2D-трансформации, transition/keyframes, `calc`/`clamp`, переменные цвета темы `--tg-theme-*`.
  Нет `vh`, `sticky`, прокрутки внутри виджета, `@font-face`, `:has()`, 3D.
- **JS:** ES2023, Promise/async, таймеры, DOM, Canvas 2D, Web Animations, `localStorage` (4 КБ на сообщение), `crypto.randomUUID`.
  Нет сети, `alert`, буфера обмена (есть `copyText`), `location`.
- **Объект `TelegramWidget`:** `chatData`, `publicData`, `viewer` (кто смотрит), `request`, `openLink`, `openTelegramLink`, `copyText`,
  `HapticFeedback`, `watchPublicData`, `wallet`, `onEvent`.
- Виджет можно приостановить и пересоздать в любой момент: после этого живут только состояние и `localStorage`.

## Кошелёк TON

- `request(data, { wallet: true })` — бот получает карточку кошелька пользователя (адрес, баланс).
- Бот отвечает транзакцией в формате TON Connect, виджет вызывает `TelegramWidget.wallet.sendTransaction(...)`.
- Результат приходит боту как `widget_query` с типом `sent_transaction`; проверять по блокчейну, а не только по хешу.
- `signData` — подпись данных, тип запроса `sign_data`.

## Где что искать в документации

| Что нужно | Где |
|---|---|
| Главный гайд по виджетам (дизайн, реализация, ограничения, объект `TelegramWidget`, тестирование) | https://core.telegram.org/bots/widgets |
| То же в машиночитаемом виде | https://core.telegram.org/bots/widgets.md |
| Готовый блок для системного промпта LLM, пишущей виджеты | раздел «Writing widgets with an LLM» на странице выше |
| Changelog 10.4 (полный список изменений) | https://core.telegram.org/bots/api-changelog#november-9-2026 |
| `sendWidget` | https://core.telegram.org/bots/api#sendwidget |
| `sendSharedWidget` | https://core.telegram.org/bots/api#sendsharedwidget |
| `answerWidgetQuery` | https://core.telegram.org/bots/api#answerwidgetquery |
| `editMessageWidgetData` | https://core.telegram.org/bots/api#editmessagewidgetdata |
| `editSharedWidgetData` | https://core.telegram.org/bots/api#editsharedwidgetdata |
| Тип `Widget` | https://core.telegram.org/bots/api#widget |
| `WidgetQuery`, `SharedWidgetQuery` | https://core.telegram.org/bots/api#widgetquery |
| Типы запросов (`data`, `sign_data`, `sent_transaction`) | https://core.telegram.org/bots/api#widgetquerytype |
| Виджеты в inline-режиме | https://core.telegram.org/bots/api#inputwidgetmessagecontent |
| Карточка кошелька TON | https://core.telegram.org/bots/api#tonwalletcard |
| Служебное сообщение о переводе TON | https://core.telegram.org/bots/api#tonwallettransfer |
| Эфемерные ответы на кнопку | https://core.telegram.org/bots/api#ephemeralmessageparameters |
| Цитаты из rich-сообщений | https://core.telegram.org/bots/api#replyparameters |
| `getUniqueGift` | https://core.telegram.org/bots/api#getuniquegift |

Не путать с https://core.telegram.org/widgets — это старые виджеты для сайтов (вход через Telegram, встраивание постов).

## Не проверено

- Полные таблицы параметров методов в Bot API читались частично (страница очень большая); точные поля `sendWidget` брать из справочника.
- Минимальная версия приложения Telegram для виджетов в документации не названа.

## Пользовательская сторона (MTProto) — проверено 2026-11-11

Виджеты есть и в MTProto (слой 233 в схемах tdesktop и tdlib; публичная страница схемы отстаёт, слой 225).
- Сообщение — `messageMediaWidget` (`payload` — HTML, сжатый gzip, плюс `public_data`, `chat_data`, `widget_id`).
- Нажатие от имени пользователя — `messages.getBotWidgetQueryAnswer(peer, msg_id, query_type, data)`.
- Обновление состояния — `messages.getWidgetPublicData`, апдейт `updateMessageWidgetData`.
- Telethon 1.45.0 (PyPI) — слой 229, виджетов нет; в исходниках Telethon (ветка `v1`) схема уже слоя 233.
- Апстрим `chigwell/telegram-mcp` виджетов не поддерживает.
