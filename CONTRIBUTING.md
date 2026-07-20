# Разработка

## Настройка окружения

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## Тесты

```bash
.venv/bin/python -m pytest
```

Тесты не ходят в сеть и не требуют настоящей Telegram-сессии.

## Сборка бандла для Claude Desktop

```bash
bash mcpb/build.sh
```

Требуется Node.js (CLI `mcpb` запускается через `npx`, ставить заранее не
нужно) и, желательно, `uv` (обновляет `mcpb/uv.lock` перед упаковкой — сборка
работает и без него, если `uv.lock` уже существует). Результат:
`dist/telegram-mcp-ag-<version>.mcpb`.

`mcpb/src/telegram_mcp_ag/` генерируется этим скриптом (копируется из
`src/telegram_mcp_ag/`) и находится в `.gitignore` — не редактируйте её
напрямую, редактируйте `src/telegram_mcp_ag/` и пересобирайте.

Скрипт откажется собирать бандл, если:
- версия в `pyproject.toml` и версия в `mcpb/manifest.json` разошлись
  (пользователь видит версию манифеста, а получает версию пакета — расхождение
  осталось бы незамеченным);
- зависимости в `mcpb/pyproject.toml` не покрывают всё, что перечислено в
  корневом `pyproject.toml` (недостающая зависимость в бандле означает
  `ImportError` при первом запуске Claude Desktop, а не на этапе сборки).

## Обновление закреплённого апстрима (`chigwell/telegram-mcp`)

Этот проект импортирует `telegram_mcp.runtime` и `telegram_mcp.runner`
напрямую — этот модуль единственная точка интеграции с апстримом, и он не
даёт гарантий совместимости между коммитами. Относитесь к каждому обновлению
пина как к ручному ревью, а не к рутинному апдейту зависимости:

1. Выберите новый коммит из
   [`chigwell/telegram-mcp`](https://github.com/chigwell/telegram-mcp).
2. Обновите пин **в обоих местах** — они всегда должны совпадать:
   - `pyproject.toml` → `dependencies` → строка `telegram-mcp @ git+...@<sha>`
   - `mcpb/pyproject.toml` → та же строка
3. Посмотрите diff между старым и новым SHA в `telegram_mcp/runtime.py` и
   `telegram_mcp/tools/`, в частности:
   - сигнатуры всего, что `src/telegram_mcp_ag/server.py` импортирует по имени
     (`ensure_connected`, `get_client`, `get_sender_name`, `resolve_entity`,
     `sanitize_user_content`, `validate_id`, `with_account`, `ToolAnnotations`,
     `json_serializer`, `log_and_format_error`, `mcp`);
   - `_apply_exposed_tools_mode` / фильтрацию по `readOnlyHint`, на которой
     держится гарантия `TELEGRAM_EXPOSED_TOOLS=read-only` — это то, на чём
     построен весь проект;
   - формат ответа `TranscribeAudioRequest`, который читает `server.py`
     (`trial_remains_num`, `trial_remains_until_date`, `pending`, `text`), и
     ключи `transcribe_audio_trial_weekly_number` /
     `transcribe_audio_trial_duration_max`, читаемые из `help.getAppConfig`.
4. Прогоните `pytest`, затем пересоберите и вручную проверьте бандл `.mcpb`
   (`bash mcpb/build.sh`, установить локально, подключить настоящий аккаунт).
5. Обновите SHA, упомянутый в `CLAUDE.md` и `PLAN.md`, если он там указан.

## Скрипты установщиков

`install.sh` (macOS/Linux) и `install.ps1` (Windows) рассчитаны на людей,
которые никогда не открывали терминал, поэтому весь пользовательский текст
в них на русском, хотя комментарии в коде — на английском. Сохраняйте это
разделение при правках. Перед коммитом проверяйте синтаксис:

```bash
bash -n install.sh
shellcheck install.sh   # если установлен
```

```powershell
Invoke-ScriptAnalyzer -Path install.ps1   # если установлен PSScriptAnalyzer
```

Оба скрипта спроектированы идемпотентными (повторный запуск `install.sh` не
должен создавать вторую регистрацию в Claude Code) — сохраняйте это свойство
при добавлении новых шагов регистрации.
