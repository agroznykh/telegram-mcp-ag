# TODO: скачивание медиафайлов, обновление апстрима, кнопки

Статус: исследовано и спроектировано (2026-11-04). Шаг 1 влит в `main`, шаг 2
реализован (итоги в конце раздела «Шаг 2»), шаг 3 не начат.

Три шага, каждый — отдельная ветка, отдельный PR и отдельный чат. Порядок
строгий: 1 → 2 → 3 (шаги 2 и 3 опираются на обновлённый апстрим).
Перед началом прочитать `CLAUDE.md` (инварианты, релиз) и этот файл целиком.

Общее для всех шагов:

- код только через PR, ветка от свежего `main`;
- тесты: `.venv/bin/python -m pytest` — зелёные до коммита;
- версию и тег не трогать без команды владельца (см. «Релиз» в `CLAUDE.md`);
  запись в `CHANGELOG.md` — по команде на релиз;
- после PR — код-ревью (`/code-review`), правки по нему — в тот же PR;
- в конце — кратко владельцу: что изменено и почему.

---

## Шаг 1. Обновить пин апстрима до v3.3.4

**Цель:** `chigwell/telegram-mcp` v3.3.4 вместо v3.2.90.

**Уже проверено при исследовании** (копия проекта с новым SHA):

- все наши тесты проходят: 122 passed, 1 skipped;
- набор инструментов в режиме `read-only` не изменился (тест
  `tests/test_upstream_surface.py` зелёный без правок);
- все символы апстрима, которые использует наш код, на месте и с теми же
  сигнатурами: `clients`, `_build_client`, `log_and_format_error`,
  `_apply_exposed_tools_mode`, `_configure_allowed_roots_from_cli`,
  `format_tool_result`, `rich_text_to_str`, `get_client`, `resolve_entity`,
  `with_account`, `validate_id`, `ensure_connected`, `get_sender_name`,
  `json_serializer`, `runner.main`;
- зависимости апстрима не менялись; Python не меняется (апстрим `>=3.10`,
  у нас 3.14; ограничение `mcp<2` у апстрима то же);
- изменения `runtime.py`, которые нас касаются, безвредны:
  - коды ошибок (`CHAT-ERR-606` и т.п.) теперь считаются через `crc32`,
    а не `hash()` — значения кодов сменятся, но станут постоянными между
    перезапусками; наши тесты на коды не завязаны;
  - `pythonjsonlogger.json.JsonFormatter` — нужен `python-json-logger>=3.1`,
    апстрим требует `>=3.3.0`;
  - `OPTIONAL_ACCOUNTS` — только для нескольких аккаунтов, у нас один.

**Что сделать:**

1. `pyproject.toml`, строка зависимости `telegram-mcp @ git+...@<SHA>`:
   SHA `6f169eacdd5582a291061ae15b4829029666f53c` →
   `0246df8c3c99818d68f50e8591b2d3dda8363f81` (тег v3.3.4). Комментарий над
   строкой: `# v3.3.4 (2026-10-31). Moved from 6f169ea (v3.2.90, 2026-10-19).`
   Остальной комментарий не трогать.
2. Переустановить: `.venv/bin/python -m pip install -e ".[dev]"`.
3. Прогнать тесты.
4. Проверить `_HIDDEN_UPSTREAM_TOOLS` в `server.py`: новых инструментов
   с `readOnlyHint=True` в v3.3.4 нет (проверено), список не меняется.
5. Живой запуск сервера в этом шаге не делать: хватает тестов. На своей
   сессии сервер проверяет владелец после релиза.

**Что получим вместе с обновлением** (для `CHANGELOG.md` при релизе):

- `download_media` скачивает медиа из постов блочного формата (rich-постов)
  — новый параметр `media_id`;
- кириллица и эмодзи в JSON-ответах — читаемым текстом, а не `\uXXXX`;
- `get_pinned_messages` возвращает все закреплённые;
- страницы `search_global` больше не повторяются, есть `next_cursor`;
- админы и баны работают в обычных (не супер-) группах;
- исправления под Windows;
- новый изменяющий инструмент `set_default_send_as` — от чьего имени аккаунт
  пишет в супергруппе (от себя или от своего канала); настройка сохраняется
  в Telegram и действует во всех клиентах до следующей смены. В `read-only`
  скрыт.

**Найдено попутно, не входит в шаг** (решает владелец): патч
`_patch_log_and_format_error` в `server.py` перебирает модули
`accounts, chats, contacts, events, folders, groups, media, messages, profile`,
а у апстрима есть ещё `custom_emoji` и `stickers` (были и в старом пине).
В их инструментах при отозванной сессии пользователь видит непонятный код
ошибки вместо подсказки о перелогине.

---

## Шаг 2. Скачивание медиа по умолчанию

**Цель:** в режиме по умолчанию работает апстримный `download_media`:
голосовые (`.ogg`), кружки и видео (`.mp4`), фото, документы. Файл
сохраняется на диск, инструмент возвращает путь.

### Как работает `download_media` (апстрим, не меняем)

Пишет только внутрь «разрешённых папок» (allowed roots). Это настройка только
нашего сервера и только его файловых инструментов. Источники:

1. **Папки клиента (MCP Roots).** Claude Code сообщает папку проекта —
   файл ложится в `<проект>/<TELEGRAM_DOWNLOAD_SUBDIR>/`. Проверено вживую.
2. **`TELEGRAM_ALLOWED_ROOTS`** — используется вместо папок клиента, если
   клиент их не сообщил: не поддерживает Roots — всегда; прислал пустой список
   или не ответил за 10 с — только при `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK=1`.
3. Без папки по умолчанию файл кладётся в
   `<первая папка>/<TELEGRAM_DOWNLOAD_SUBDIR>/`, а если имя первой папки
   совпадает с `TELEGRAM_DOWNLOAD_SUBDIR` (без учёта регистра) — прямо в неё.
   По умолчанию у апстрима подпапка `downloads`.
4. Апстрим при старте создаёт каждую папку из `TELEGRAM_ALLOWED_ROOTS`
   (`mkdir`), а если не может — завершает процесс (`SystemExit`). Это
   учтено в п. «Защита от недоступной папки» ниже.

### Решения владельца

- Основная папка — папка проекта, которую сообщает клиент.
- Запасная — `Загрузки/Telegram` (системная папка загрузок пользователя).
- `TELEGRAM_SERVER_ROOTS_ONLY` не задаём.
- Режим по умолчанию — `read-only+download_media` (проверено: добавляет ровно
  один инструмент `download_media` к `read-only`).
- У уже установленных: установщик меняет **ровно** `read-only` на
  `read-only+download_media`; любое другое значение не трогает.

### Значения настроек

| Ключ | Значение | Зачем |
|---|---|---|
| `TELEGRAM_EXPOSED_TOOLS` | `read-only+download_media` | включает скачивание |
| `TELEGRAM_ALLOWED_ROOTS` | абсолютный путь `<Загрузки>/Telegram` | запасная папка |
| `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK` | `1` | пустой список и молчание клиента тоже ведут в запасную папку |
| `TELEGRAM_DOWNLOAD_SUBDIR` | `Telegram` | в проекте файлы ложатся в `<проект>/Telegram/`, в запасной папке — прямо в `<Загрузки>/Telegram/`, без лишней `downloads/` внутри |

`<Загрузки>` по ОС:

- macOS: `~/Downloads`;
- Windows: известная папка Downloads через `SHGetKnownFolderPath`
  (FOLDERID_Downloads `{374DE290-123F-4565-9164-39C4925E467B}`, `ctypes`,
  `shell32`); при любой ошибке — `~/Downloads`. Пользователь мог перенести
  Загрузки на другой диск, поэтому не `~/Downloads` напрямую;
- Linux: `XDG_DOWNLOAD_DIR` из `~/.config/user-dirs.dirs` (формат строки:
  `XDG_DOWNLOAD_DIR="$HOME/Загрузки"`, подставить `$HOME`); нет файла или
  ключа — `~/Downloads`. В русской локали папка называется `Загрузки`.

### Что сделать

1. **`config.py`:**
   - функция `default_download_root() -> Path` — `<Загрузки>/Telegram` по
     правилам выше; папку не создаёт;
   - `_ensure_exposed_tools_line` → значение по умолчанию
     `read-only+download_media` вместо `read-only` (строка пишется, только
     если ключа нет в файле — поведение не меняется);
   - новая `_ensure_download_lines(config_path)` по образцу
     `_ensure_exposed_tools_line`: для каждого из трёх ключей
     `TELEGRAM_ALLOWED_ROOTS`, `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK`,
     `TELEGRAM_DOWNLOAD_SUBDIR` — дописать, если ключа нет в файле. Ключ уже
     есть (даже с другим или пустым значением) — решение пользователя, не
     трогать. Файл не записать — не ошибка. Вызывать из `load()` сразу после
     `_ensure_exposed_tools_line`.
2. **`server.py`:**
   - значение по умолчанию при пустом `TELEGRAM_EXPOSED_TOOLS` (строка
     `os.environ["TELEGRAM_EXPOSED_TOOLS"] = "read-only"`) →
     `read-only+download_media`; поправить комментарий над ним;
   - так же, до импорта `telegram_mcp`: если `TELEGRAM_ALLOWED_ROOTS`,
     `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK`, `TELEGRAM_DOWNLOAD_SUBDIR` пусты —
     подставить значения из таблицы (на случай, если `config.env` записать
     не удалось);
   - **защита от недоступной папки:** функция
     `_drop_unusable_allowed_roots()`, вызывать в `main()` первой строкой,
     до `_runner.main()` и до `_configure_allowed_roots_from_cli` в режиме
     входа. Разбирает `TELEGRAM_ALLOWED_ROOTS` через
     `_runtime._parse_allowed_roots_env`, для каждой папки:
     `Path(...).expanduser()`, `mkdir(parents=True, exist_ok=True)`,
     проверка `is_dir()` и `os.access(..., os.W_OK)`. Папки, на которых
     что-то из этого упало (`OSError`), выкинуть с сообщением в stderr:
     `telegram-mcp-ag: папка для скачанных файлов недоступна (<путь>): <ошибка>.
     Скачивание будет работать только в папке проекта.` Оставшиеся записать
     обратно в env через `;`, если не осталось ни одной — удалить
     переменную. Зачем: на macOS папка Загрузки защищена системой; если
     пользователь отказал клиенту в доступе, апстрим иначе не запустится
     вовсе (`SystemExit` при старте).
3. **`install.sh`** (`write_config_env`) и **`install.ps1`**
   (`Write-ConfigEnv`): в свежий `config.env` писать
   `TELEGRAM_EXPOSED_TOOLS=read-only+download_media`. Три ключа папок
   установщики не пишут — их допишет `config.py` при первом старте сервера
   (один источник правды о пути Загрузок — Python).
4. **Миграция в установщиках** — новый шаг, выполняется при каждом запуске
   установщика после установки пакета, независимо от того, есть ли сессия
   (в `main()` перед `register_claude_code`; в `Invoke-Main` — в том же
   месте):
   - если в `config.env` есть строка ровно `TELEGRAM_EXPOSED_TOOLS=read-only`
     (допустимы кавычки вокруг значения и пробелы по краям) — заменить
     значение на `read-only+download_media`, права файла оставить `600`,
     сообщить: `[i] Включено скачивание медиафайлов из Telegram (голосовые,
     кружки, видео). Отключить: в config.env замените read-only+download_media
     на read-only.`;
   - любое другое значение — не трогать, ничего не писать;
   - bash: без `sed -i` (у macOS и Linux разный синтаксис) — читать и писать
     через временный файл в той же папке и `mv`; учесть bash 3.2 на macOS
     (см. `install_claude_skills` — изоляция в подшелле).
5. **`config.env.example`:** `TELEGRAM_EXPOSED_TOOLS=read-only+download_media`
   и три ключа папок с комментариями: путь-пример для каждой ОС, что значит
   каждый ключ.
6. **Тесты:**
   - `tests/test_upstream_surface.py`: режим по умолчанию (пустое значение)
     даёт `EXPECTED_READ_ONLY_TOOLS | {"download_media"}`; режим `read-only`
     по-прежнему без `download_media`; поправить docstring/комментарии;
   - `tests/test_config.py`: `default_download_root` для трёх ОС (через
     monkeypatch `sys.platform`/`os.name`, `Path.home`, файл `user-dirs.dirs`
     во временной папке, сбой `SHGetKnownFolderPath`); `_ensure_download_lines`
     дописывает отсутствующие ключи и не трогает существующие (включая пустое
     значение); новое значение по умолчанию `TELEGRAM_EXPOSED_TOOLS`;
   - `tests/test_server.py`: `_drop_unusable_allowed_roots` — доступная папка
     остаётся; папка, где `mkdir` бросает `PermissionError`, выкидывается;
     все недоступны — переменная удалена;
   - `tests/test_installer_syntax.py`: если там проверяется содержимое
     установщиков — дополнить миграцией.
7. **Документация:**
   - `README.md` — что умеет (скачивание голосовых, кружков, видео; куда
     ложатся файлы: в папку `Telegram` текущего проекта, а если клиент папку
     не сообщает — в `Загрузки/Telegram`); на macOS при первом запуске
     система может спросить доступ к папке «Загрузки» от имени Claude / ChatGPT /
     терминала — разрешить; как отключить;
   - `TECHNICAL.md` — режим `read-only+download_media`, четыре ключа и их
     смысл, защита от недоступной папки, миграция в установщике;
   - `CLAUDE.md` проекта, инвариант 1 — дописать: «Исключение —
     `download_media`: пишет файл на диск пользователя, в Telegram ничего
     не меняет».
8. **Скилл `telegram-digest`** (`.claude/skills/telegram-digest/SKILL.md`):
   абзац — если пользователь просит сам файл голосового / кружка / видео,
   вызвать `download_media(chat_id, message_id)` и сообщить путь; для
   rich-поста с несколькими медиа — `media_id`. Пересобрать архив
   `.claude/skills/build_zip.py` (имя и версия архива — как в
   `tests/test_skill_zip.py`).

**Проверить на живых клиентах после релиза** (владелец): Claude Desktop
(Chat, Cowork), Codex CLI, ChatGPT Desktop — куда легли файлы; на macOS —
появляется ли запрос доступа к Загрузкам и что будет при отказе.

### Итоги шага 2 (2026-11-04)

Реализовано, но **отличается от плана выше** — по итогам код-ревью (Opus) и решениям
владельца. Актуально именно это, а не текст плана:

- **Скачивание выключено по умолчанию.** Умолчание `TELEGRAM_EXPOSED_TOOLS` — снова
  `read-only`; `read-only+download_media` включается вручную строкой в `config.env`.
  Причина: у апстримного `download_media` есть `file_path` (запись куда угодно внутри
  папки проекта, перезапись существующего), а установщики заранее одобряют вызовы
  инструментов сервера. Риск описан в `SECURITY.md` и `README.md`, `file_path` не
  ограничивали.
- **Миграции в установщиках нет.** Пункт 4 плана (замена `read-only` на
  `read-only+download_media`) убран: установщик не отличает старое умолчание от
  осознанного выбора. `install.sh` и `install.ps1` в итоге не изменены.
- **Папки только при включённом скачивании.** `config.download_media_enabled()`
  повторяет разбор режима апстримом (`all`, `read-only+…`, `-…`; тест сверяет с
  апстримом). Ключи `TELEGRAM_ALLOWED_ROOTS`, `TELEGRAM_ALLOW_SERVER_ROOTS_FALLBACK`,
  `TELEGRAM_DOWNLOAD_SUBDIR` пишутся в `config.env` и подставляются в окружение только
  тогда, когда `download_media` реально доступен. В обычном `read-only` папка не
  создаётся и системный запрос macOS не появляется. `FALLBACK=1` оставлен.
- **Пустой ключ папок** сервер считает «не задано» (инвариант 7) и берёт умолчание:
  отключить запасную папку пустым значением нельзя, только выключить `download_media`.
- **Запасная папка не предлагается**, если её путь содержит `;`, `,` или `:` не после
  буквы диска: апстрим делит список по этим символам.
- **Linux:** `default_download_root()` учитывает `XDG_CONFIG_HOME` и `${HOME}`.
- Сделано по плану: `default_download_root()`, `_drop_unusable_allowed_roots()` (первая
  строка `main()`), `config.env.example`, документация, скилл `telegram-digest` 1.3 с
  пересобранным архивом, тесты.
- Тесты теперь работают с временным `HOME` (`tests/conftest.py`): раньше импорт сервера
  писал в настоящий `config.env` разработчика.
- Ревью: код-ревью (Opus, `xhigh`) — 15 находок, исправлены или закрыты решениями
  владельца; ревью безопасности — находок нет.
- Версия, тег и `CHANGELOG.md` не тронуты (по команде владельца на релиз). В
  `CHANGELOG.md` при релизе: скачивание медиа — опция, включается вручную.

---

## Шаг 3. Кнопки: перенять у апстрима недостающее

**Цель:** наши `list_inline_buttons` / `press_inline_button`
(`src/telegram_mcp_ag/rich_buttons.py`) умеют всё, что умеют апстримные
v3.3.4, и сохраняют то, чего у апстрима нет (кнопки rich-постов).

**Почему своё, а не апстримное:** апстримный `telegram_mcp/keyboards.py` не
видит кнопки rich-постов. Импортировать его нельзя — инвариант 3: единственная
точка интеграции с апстримом — `runtime.py`. Логику переносим в наш модуль.
Образец — `telegram_mcp/keyboards.py` и функции `_keyboard_message`,
`_choose_button`, `_unpressable_reason`, `press_inline_button` в
`telegram_mcp/tools/messages.py` апстрима v3.3.4; тесты-образцы —
`tests/test_inline_buttons.py` апстрима.

### Что сейчас есть у нас и чего нет у апстрима — сохранить

- кнопки rich-постов (`PageButton` в `message.rich_message.blocks`),
  дозапрос полного rich-сообщения (`_full_rich_message`);
- rich-кнопки и клавиатура в одном сообщении — одна нумерация;
- стиль — списком сырых флагов `style: ["bg_success", ...]`, включая `link`;
- поиск сообщения с кнопками в последних 30, если по id клавиатура потерялась;
- отказ на кнопке с паролем 2FA с нашим текстом (пароль не идёт через чат —
  инвариант 9).

### Что добавить

1. **Строка и столбец** — поля `row`, `col` у каждой кнопки, номера с нуля.
   Rich-кнопки: строка — каждый `PageBlockButtonRow` в порядке обхода блоков;
   `PageButton` вне `PageBlockButtonRow` — отдельная строка. Клавиатура:
   строки `reply_markup.rows`. Нумерация строк сквозная по сообщению:
   сначала строки rich-кнопок, затем строки клавиатуры. `col` — позиция
   внутри строки.
2. **Обычная клавиатура бота** (`ReplyKeyboardMarkup` — кнопки вместо поля
   ввода). Читать клавиатуру из сырого `message.reply_markup`, а не из
   `message.buttons`: `ReplyInlineMarkup` → `keyboard: "inline"`,
   `ReplyKeyboardMarkup` → `keyboard: "reply"`, любая другая разметка
   (скрыть клавиатуру, `force_reply`) — кнопок нет. В метаданные ответа
   `list_inline_buttons` добавить `keyboard` (`inline`/`reply`, если
   клавиатура есть), а для `reply` ещё `placeholder` (через
   `sanitize_user_content`, до 256) и `single_use`.
3. **`kind` по типу кнопки** — одна таблица для rich-, inline- и
   reply-кнопок (как `_KINDS` апстрима):
   `InlineButtonTypeCallback` callback, `…Url` url, `…SwitchInline`
   switch_inline, `…Game` game, `…Buy` buy, `…UrlAuth` url_auth, `…WebView`
   web_view, `…Copy` copy, `…UserProfile` user_profile, `…Disabled` disabled,
   `ButtonTypeDefault` text, `ButtonTypeRequestPhone` request_phone,
   `ButtonTypeRequestGeoLocation` request_location, `ButtonTypeRequestPoll`
   request_poll, `ButtonTypeRequestPeer` request_peer, `ButtonTypeSimpleWebView`
   web_app; иначе — `unknown` и поле `type` с именем класса. Для inline-кнопок
   значения совпадают с нынешними — совместимость сохраняется. Кнопка без
   поля `type` (старый слой) — как сейчас: есть `data` — callback, есть
   `url` — url, иначе `unknown`.
4. **Поля по типу** (как `Button.record()` апстрима, весь текст — через
   `sanitize_user_content`, до 256):
   `url` — для url/url_auth/web_view/web_app; `query` и `same_peer` —
   switch_inline; `copy_text` — copy; `user_id` — user_profile; `quiz` —
   request_poll, если задан; `requires_password` — callback с паролем.
   `has_callback` — true только для callback без пароля (как сейчас).
5. **Значок кнопки** — `icon_emoji_id` (строкой) из `style.icon`
   у клавиатурных кнопок (`KeyboardButtonStyle`). У rich-кнопок
   (`RichButtonStyle`) значка нет.
6. **Нажатие** (`press_inline_button`):
   - callback без пароля — `GetBotCallbackAnswerRequest(data=...)`, как сейчас;
   - game — `GetBotCallbackAnswerRequest(peer, msg_id, game=True)`;
   - url / url_auth / web_view / web_app — не нажимать, вернуть
     `Selected button opens a URL instead of sending a callback: <url>`;
   - copy — не нажимать, вернуть
     `Selected button copies text instead of sending a callback: <copy_text>`;
   - text (reply-клавиатура) — не нажимать: `Selected button is on a reply
     keyboard: pressing it sends its text as a message. Use send_message with
     the button text instead (not available in read-only mode).`;
   - request_phone / request_location — не нажимать, отдельный текст:
     `This button would share your phone number / location with the bot.
     It is not pressed from here: press it in a Telegram app if you mean it.`;
   - request_poll, request_peer, buy, switch_inline, user_profile, unknown —
     как сейчас: «acts on the client side, nothing to press»;
   - disabled и пароль 2FA — наши нынешние тексты без изменений.
7. **Ответ на нажатие:** метаданные `response` (текст бота или
   `Button pressed successfully.`), `alert` (bool, всегда), `url`
   (если бот вернул). Если есть текст и `alert` — дописать
   ` Telegram displayed an alert to the user.`
8. **Ошибки нажатия** — поймать и вернуть текстом (как апстрим):
   `BotResponseTimeoutError` → `The bot did not answer the button press in
   time. The press may still have reached it: check the chat before pressing
   again.`; `DataInvalidError` → `Telegram refused the button's data; the
   keyboard has probably changed. Call list_inline_buttons again.`;
   `MessageIdInvalidError` → `Message <id> is no longer in this chat.`
9. **Docstring'и** обоих инструментов — перечислить: inline, reply и
   rich-кнопки; поля `row`, `col`, `kind`, `keyboard`; что нажимается
   (callback, game) и что нет.

### Тесты (`tests/test_rich_buttons.py`)

Существующие должны остаться зелёными (кроме тех, что проверяют чтение через
`message.buttons` / Telethon-обёртку `MessageButton`: их переписать на
`reply_markup`, смысл проверки сохранить). Добавить:

- `row`/`col` у rich-строк, у клавиатуры и сквозная нумерация в смешанном
  сообщении;
- reply-клавиатура: `keyboard: "reply"`, `placeholder`, `single_use`,
  `kind` для text / request_phone / request_location / request_poll /
  request_peer / web_app;
- поля `query`/`same_peer`, `copy_text`, `user_id`, `quiz`, `icon_emoji_id`,
  `type` для unknown;
- нажатие game — запрос с `game=True`;
- отказы: url (все четыре вида), copy, text, request_phone, request_location;
- ответ с `url` и `alert`;
- три ошибки нажатия.

### Документация

- `CHANGELOG.md`, «Что умеет сейчас» — строку про кнопки обновить при релизе;
- `TECHNICAL.md` — если там описан формат ответа кнопок, обновить.

---

## Справка: зачем недостающее в кнопках

- **Строка и столбец** — сетки: календарь записи, выбор времени, цифровая
  клавиатура; ассистент понимает «вторая во втором ряду».
- **Обычная клавиатура бота** — меню многих ботов («📋 Меню», «Записаться»)
  сделаны так; ассистент увидит пункты и подскажет, что отправить.
- **Copy-кнопки** — промокод, адрес кошелька: ассистент получит сам текст.
- **Ссылка в ответе бота** — бот на нажатие открывает сайт или другого бота.
- **Игровые кнопки** — боты-игры, редкость.
