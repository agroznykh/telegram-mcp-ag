# Telegram MCP Chigwell

Локально устанавливаемый MCP-сервер для Codex и Claude Code. Он использует
`chigwell/telegram-mcp` как upstream и добавляет read-only расшифровку голосовых,
аудио- и video-note сообщений через нативный Telegram API.

## Возможности

- чтение Telegram через upstream `telegram-mcp`;
- read-only режим по умолчанию;
- `transcribe_voice_message` для одного сообщения;
- `transcribe_voice_messages` для пакетной расшифровки с ограничениями времени
  и размера;
- запуск одной командой `telegram-mcp-chigwell`.

Отправка сообщений и другие изменяющие действия намеренно не включены в
конфигурации этого проекта.

## Установка

Нужен Python 3.10 или новее. В отдельной папке для установки создайте окружение:

```bash
mkdir -p ~/projects/telegram-mcp-chigwell
cd ~/projects/telegram-mcp-chigwell
python3.12 -m venv .venv
```

Пока пакет не опубликован в GitHub, установите собранный wheel:

```bash
.venv/bin/python -m pip install /absolute/path/to/telegram_mcp_chigwell-0.1.0-py3-none-any.whl
```

При публикации в GitHub установка будет выглядеть так, без ручного клонирования:

```bash
.venv/bin/python -m pip install "git+https://github.com/<owner>/telegram-mcp-chigwell.git@v0.1.0"
```

Пакет сам устанавливает проверенную ревизию `chigwell/telegram-mcp` напрямую из
GitHub. Он не использует одноимённый пакет с PyPI.

## Telegram-вход

Сгенерируйте сессию в том же окружении:

```bash
.venv/bin/telegram-mcp-generate-session --qr
```

В Telegram откройте `Settings -> Devices -> Link Desktop Device` и отсканируйте
QR-код. Не добавляйте `TELEGRAM_SESSION_STRING` в репозиторий и не отправляйте её
в чат.

## Codex

Скопируйте образец из [examples/codex.config.toml](examples/codex.config.toml) в
project-scoped `.codex/config.toml`, замените путь к команде и значения
`TELEGRAM_*` только локально. После изменения создайте новый тред или
перезапустите Codex.

## Claude Code

Скопируйте [examples/claude-code.mcp.json](examples/claude-code.mcp.json) в
`.mcp.json` нужного проекта либо добавьте эквивалентную конфигурацию через
`claude mcp add`. Замените локальные плейсхолдеры пути и Telegram-переменных.

## Проверка

После настройки попросите клиента перечислить аккаунты или показать непрочитанные
диалоги, не отмечая сообщения прочитанными. Расшифровка требует доступа Telegram
к функции транскрибации, который обычно есть у Premium-аккаунтов.

## Разработка

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip wheel --wheel-dir dist .
```

## Безопасность

- Оставляйте `TELEGRAM_EXPOSED_TOOLS = "read-only"`.
- Сессия Telegram равна доступу к вашему аккаунту.
- Используйте отдельный MCP-проект, чтобы Telegram-инструменты не попадали в
  обычные рабочие треды.
- Для отзыва доступа завершите соответствующую сессию в Telegram `Settings -> Devices`.

## Лицензии

Эта надстройка распространяется под Apache-2.0. Upstream
[`chigwell/telegram-mcp`](https://github.com/chigwell/telegram-mcp) также
лицензирован под Apache-2.0.
