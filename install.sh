#!/usr/bin/env bash
# Installer for telegram-mcp-ag (macOS / Linux).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.sh | bash
#   bash install.sh --relogin     # redo the Telegram login, keep everything else
#   bash install.sh --uninstall   # remove client registrations and (optionally) the install dir
#
# All user-facing text is Russian: this installer targets people who have
# never opened a terminal before (see CLAUDE.md). Code comments are English.
set -euo pipefail

REPO_URL="https://github.com/agroznykh/telegram-mcp-ag.git"
REPO_API="https://api.github.com/repos/agroznykh/telegram-mcp-ag/releases/latest"
# Override for testing: TELEGRAM_MCP_AG_REF=some-branch bash install.sh
# Resolved below, once the output helpers it might warn through are defined.
REPO_REF=""

# Must match telegram_mcp_ag.config.CONFIG_DIR exactly -- it is not
# configurable, so this path cannot be overridden here either.
INSTALL_DIR="$HOME/telegram-mcp-ag"
VENV_DIR="$INSTALL_DIR/.venv"
CONFIG_PATH="$INSTALL_DIR/config.env"
SERVER_NAME="telegram-mcp-ag"

# Filled in by prompt_api_credentials()/run_telegram_login() before
# run_telegram_login()/write_config_env() read them. Pre-declared (empty)
# so `set -u` doesn't complain if that ever changes.
API_ID=""
API_HASH=""
SESSION_VAR=""
SESSION_STRING=""
OS=""
ARCH=""

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
    C_INFO="$(tput setaf 4)"
    C_OK="$(tput setaf 2)"
    C_WARN="$(tput setaf 3)"
    C_ERR="$(tput setaf 1)"
    C_RESET="$(tput sgr0)"
else
    C_INFO=""
    C_OK=""
    C_WARN=""
    C_ERR=""
    C_RESET=""
fi

info() { printf '%s[i]%s %s\n' "$C_INFO" "$C_RESET" "$1"; }
ok() { printf '%s[v]%s %s\n' "$C_OK" "$C_RESET" "$1"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_RESET" "$1" >&2; }
error() { printf '%s[x]%s %s\n' "$C_ERR" "$C_RESET" "$1" >&2; }

trap 'error "Установка прервана из-за ошибки (строка $LINENO). Проблему можно почитать выше; после исправления просто запустите install.sh ещё раз -- он не оставляет дубликатов."' ERR

# Installs the latest tagged release by default, falling back to `main` if no
# release exists yet or the GitHub API call fails (offline, rate-limited).
# `|| true` on the capture matters: with `pipefail`, a curl failure inside it
# would otherwise trip the ERR trap above and abort the whole install over
# something that has a perfectly good fallback.
_resolve_repo_ref() {
    if [[ -n "${TELEGRAM_MCP_AG_REF:-}" ]]; then
        printf '%s' "$TELEGRAM_MCP_AG_REF"
        return
    fi
    local tag=""
    tag="$(curl -fsSL "$REPO_API" 2>/dev/null | grep -o '"tag_name": *"[^"]*"' | head -1 | sed 's/.*"tag_name": *"\([^"]*\)"/\1/')" || true
    printf '%s' "${tag:-main}"
}
REPO_REF="$(_resolve_repo_ref)"

# Paths of any throwaway venvs _maintenance_python() creates (one per
# line). A file, not a shell variable, because it must survive the
# command-substitution subshell that every "py=$(_maintenance_python)"
# call runs in.
MAINTENANCE_TMP_LOG="$(mktemp)"
_cleanup_maintenance_venv() {
    if [[ -s "$MAINTENANCE_TMP_LOG" ]]; then
        while IFS= read -r dir; do
            [[ -n "$dir" ]] && rm -rf "$dir"
        done <"$MAINTENANCE_TMP_LOG"
    fi
    rm -f "$MAINTENANCE_TMP_LOG"
    return 0
}
trap _cleanup_maintenance_venv EXIT

ask() {
    # $1 = prompt text, $2 = name of the variable to fill.
    # reply must default to "": under `set -u`, a bare `local reply`
    # (no assignment) is NOT considered set, so a failed/interrupted
    # `read` below would otherwise crash this on "unbound variable"
    # instead of behaving like an empty answer.
    local reply=""
    read -r -p "$1" reply </dev/tty
    printf -v "$2" '%s' "$reply"
}

confirm() {
    local reply=""
    read -r -p "$1 [y/N] " reply </dev/tty || true
    [[ "$reply" =~ ^[YyДд]$ ]]
}

# ---------------------------------------------------------------------------
# Environment checks
# ---------------------------------------------------------------------------

detect_os() {
    OS="$(uname -s)"
    ARCH="$(uname -m)"
    case "$OS" in
        Darwin | Linux) ;;
        *)
            error "Этот установщик поддерживает только macOS и Linux. Для Windows используйте install.ps1."
            exit 1
            ;;
    esac
    info "ОС: $OS ($ARCH)"
}

require_tty() {
    if [[ ! -r /dev/tty ]]; then
        error "Нужен интерактивный терминал: установщик спрашивает api_id/api_hash и просит войти в Telegram."
        error "Скачайте скрипт и запустите его напрямую: curl -fsSL <ссылка на install.sh> -o install.sh && bash install.sh"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Python / venv setup (see PLAN.md decision 6: uv is an accelerator, not a
# requirement -- system Python is always preferred when it already satisfies
# the version floor).
# ---------------------------------------------------------------------------

_python_version_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null
}

find_system_python() {
    local candidates=(python3.13 python3.12 python3.11 python3.10 python3 python)
    local c=""
    for c in "${candidates[@]}"; do
        if command -v "$c" >/dev/null 2>&1 && _python_version_ok "$c"; then
            command -v "$c"
            return 0
        fi
    done
    return 1
}

install_uv() {
    info "Устанавливаю uv (без sudo, в ~/.local/bin)..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        ok "uv установлен."
    else
        warn "Не удалось установить uv."
    fi
}

setup_python_and_venv() {
    local py=""
    py="$(find_system_python || true)"

    if [[ -z "$py" ]] && ! command -v uv >/dev/null 2>&1; then
        warn "Не нашёл на этой машине Python версии 3.10 или новее."
        if confirm "Установить менеджер uv -- он сам скачает подходящий Python без sudo?"; then
            install_uv
        fi
    fi

    if [[ -z "$py" ]] && ! command -v uv >/dev/null 2>&1; then
        error "Нужен Python 3.10+. Установите его с https://www.python.org/downloads/ и запустите установщик ещё раз."
        exit 1
    fi

    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"

    rm -rf "$VENV_DIR"
    if [[ -n "$py" ]]; then
        info "Создаю виртуальное окружение ($py)..."
        "$py" -m venv "$VENV_DIR"
    else
        info "Создаю виртуальное окружение через uv (при необходимости скачает Python 3.12)..."
        uv venv --seed --python 3.12 "$VENV_DIR" >/dev/null
    fi
    ok "Окружение готово: $VENV_DIR"
}

install_package() {
    info "Устанавливаю telegram-mcp-ag (ref: $REPO_REF)..."
    "$VENV_DIR/bin/python" -m pip install --upgrade pip -q
    "$VENV_DIR/bin/python" -m pip install -q "git+${REPO_URL}@${REPO_REF}"
    ok "Пакет установлен."
}

# ---------------------------------------------------------------------------
# Telegram credentials + login
# ---------------------------------------------------------------------------

prompt_api_credentials() {
    echo
    info "Понадобятся api_id и api_hash с https://my.telegram.org/apps"
    info "(войдите под своим номером телефона, откройте \"API development tools\")."
    echo

    while true; do
        ask "api_id: " API_ID
        [[ "$API_ID" =~ ^[0-9]+$ ]] && break
        warn "api_id -- это число, попробуйте ещё раз."
    done

    while true; do
        ask "api_hash: " API_HASH
        if [[ -z "$API_HASH" ]]; then
            warn "api_hash не может быть пустым."
            continue
        fi
        if [[ ! "$API_HASH" =~ ^[0-9a-fA-F]{32}$ ]]; then
            warn "Обычно api_hash -- это 32 шестнадцатеричных символа. Введённое значение выглядит иначе."
            confirm "Использовать его как есть?" && break
            continue
        fi
        break
    done
}

run_telegram_login() {
    echo
    info "Вход в Telegram: сейчас появится QR-код."
    info "В приложении Telegram: Настройки -> Устройства -> Подключить устройство -> отсканировать QR-код."
    warn "На вопрос генератора сессии \"Would you like to automatically update your .env file? (y/N)\" в конце ответьте n -- этот установщик сам запишет config.env."
    echo

    local log_file=""
    log_file="$(mktemp)"
    local login_ok=0

    PYTHONUNBUFFERED=1 TELEGRAM_API_ID="$API_ID" TELEGRAM_API_HASH="$API_HASH" \
        "$VENV_DIR/bin/telegram-mcp-generate-session" --qr <"/dev/tty" | tee "$log_file" || login_ok=1

    if [[ "$login_ok" -ne 0 ]]; then
        warn "Вход по QR-коду не удался, пробую вход по номеру телефона."
        : >"$log_file"
        login_ok=0
        PYTHONUNBUFFERED=1 TELEGRAM_API_ID="$API_ID" TELEGRAM_API_HASH="$API_HASH" \
            "$VENV_DIR/bin/telegram-mcp-generate-session" --phone <"/dev/tty" | tee "$log_file" || login_ok=1
    fi

    # A stray plaintext .env may appear here if the login script's own
    # "update .env?" prompt was answered y. We already parse the session
    # string from the transcript above, so this file is redundant and, at
    # default permissions, not protected the way config.env is -- remove it.
    rm -f "$INSTALL_DIR/.env"

    if [[ "$login_ok" -ne 0 ]]; then
        rm -f "$log_file"
        error "Не удалось войти в Telegram. Проверьте api_id/api_hash и запустите: bash install.sh --relogin"
        exit 1
    fi

    # The generator names the variable TELEGRAM_SESSION_STRING by default,
    # or TELEGRAM_SESSION_STRING_<LABEL> if a label was typed at its prompt.
    local session_line=""
    session_line="$(grep -m1 -E '^TELEGRAM_SESSION_STRING(_[A-Za-z0-9_]+)?=' "$log_file" || true)"
    rm -f "$log_file"

    if [[ -z "$session_line" ]]; then
        error "Не удалось получить строку сессии из вывода входа."
        exit 1
    fi

    SESSION_VAR="${session_line%%=*}"
    SESSION_STRING="${session_line#*=}"
}

has_full_config() {
    local f="$1"
    [[ -f "$f" ]] || return 1
    grep -qE '^TELEGRAM_API_ID=.+' "$f" &&
        grep -qE '^TELEGRAM_API_HASH=.+' "$f" &&
        grep -qE '^TELEGRAM_SESSION_STRING[A-Za-z0-9_]*=.+' "$f"
}

write_config_env() {
    mkdir -p "$INSTALL_DIR"
    if [[ -f "$CONFIG_PATH" ]]; then
        cp "$CONFIG_PATH" "$CONFIG_PATH.bak-$(date +%Y%m%d%H%M%S)"
    fi

    local device_model=""
    device_model="telegram-mcp-ag ($(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown))"

    (
        umask 077
        {
            printf 'TELEGRAM_API_ID=%s\n' "$API_ID"
            printf 'TELEGRAM_API_HASH=%s\n' "$API_HASH"
            printf '%s=%s\n' "$SESSION_VAR" "$SESSION_STRING"
            printf 'TELEGRAM_EXPOSED_TOOLS=read-only\n'
            printf 'TELEGRAM_DEVICE_MODEL=%s\n' "$device_model"
        } >"$CONFIG_PATH"
    )
    chmod 600 "$CONFIG_PATH"
    ok "config.env записан ($CONFIG_PATH, права 600)."
}

# ---------------------------------------------------------------------------
# Client registration
# ---------------------------------------------------------------------------

register_claude_code() {
    if ! command -v claude >/dev/null 2>&1; then
        return 0
    fi
    info "Обнаружен Claude Code -- регистрирую сервер..."
    claude mcp remove -s user "$SERVER_NAME" >/dev/null 2>&1 || true
    if claude mcp add -s user "$SERVER_NAME" -- "$VENV_DIR/bin/telegram-mcp-ag" >/dev/null; then
        ok "Claude Code настроен (scope: user)."
    else
        warn "Не удалось зарегистрировать сервер в Claude Code. Добавьте вручную: claude mcp add -s user $SERVER_NAME -- $VENV_DIR/bin/telegram-mcp-ag"
    fi
}

# Prints the path to a usable Python, or nothing if none could be found --
# callers must check for an empty result themselves. (Never returns
# non-zero: "py=$(_maintenance_python)" is a plain assignment, and a
# failing command substitution on the right-hand side of an unguarded
# assignment trips `set -e` even outside any if/while/&&.)
_maintenance_python() {
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        printf '%s' "$VENV_DIR/bin/python"
        return 0
    fi

    # $VENV_DIR is gone (e.g. a second --uninstall run after the directory
    # was already removed), but we may still need to edit a client config.
    # System Python on Debian/Ubuntu and similar refuses `pip install`
    # outside a venv (PEP 668), so spin up a throwaway one instead of
    # touching system packages.
    local sys_py=""
    sys_py="$(find_system_python || true)"
    [[ -z "$sys_py" ]] && return 0

    local venv_root=""
    venv_root="$(mktemp -d)"
    if ! "$sys_py" -m venv "$venv_root/venv" >/dev/null 2>&1; then
        rm -rf "$venv_root"
        return 0
    fi
    echo "$venv_root" >>"$MAINTENANCE_TMP_LOG"
    printf '%s' "$venv_root/venv/bin/python"
}

_codex_toml_upsert() {
    local codex_config="$1" py=""
    py="$(_maintenance_python)"
    if [[ -z "$py" ]]; then
        warn "Не нашёл Python для правки $codex_config, пропускаю."
        return 1
    fi
    "$py" -m pip install -q tomlkit || {
        warn "Не удалось установить tomlkit, пропускаю правку $codex_config."
        return 1
    }

    mkdir -p "$(dirname "$codex_config")"
    [[ -f "$codex_config" ]] && cp "$codex_config" "$codex_config.bak-$(date +%Y%m%d%H%M%S)"

    "$py" - "$codex_config" "$VENV_DIR/bin/telegram-mcp-ag" "$SERVER_NAME" <<'PYEOF'
import sys
import tomlkit

config_path, command_path, server_name = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(config_path, "r", encoding="utf-8") as f:
        doc = tomlkit.parse(f.read())
except FileNotFoundError:
    doc = tomlkit.document()

servers = doc.setdefault("mcp_servers", tomlkit.table())
entry = tomlkit.table()
entry["command"] = command_path
entry["args"] = tomlkit.array()
servers[server_name] = entry

with open(config_path, "w", encoding="utf-8") as f:
    f.write(tomlkit.dumps(doc))
PYEOF
    ok "config.toml обновлён ($codex_config)."
}

register_codex() {
    local codex_dir="$HOME/.codex"
    local codex_config="$codex_dir/config.toml"

    if ! command -v codex >/dev/null 2>&1 && [[ ! -d "$codex_dir" ]]; then
        return 0
    fi
    info "Обнаружен Codex CLI / ChatGPT Desktop -- регистрирую сервер..."

    if command -v codex >/dev/null 2>&1; then
        codex mcp remove "$SERVER_NAME" >/dev/null 2>&1 || true
        if codex mcp add "$SERVER_NAME" -- "$VENV_DIR/bin/telegram-mcp-ag" >/dev/null 2>&1; then
            ok "Codex настроен через 'codex mcp add'."
            return 0
        fi
        warn "'codex mcp add' не сработал (команда экспериментальная), правлю $codex_config напрямую."
    fi

    _codex_toml_upsert "$codex_config" || warn "Codex/ChatGPT Desktop не настроены автоматически. Смотрите examples/codex.config.toml."
}

_claude_desktop_candidates() {
    printf '%s\n' \
        "$HOME/Library/Application Support/Claude" \
        "$HOME/.config/Claude"
}

_claude_desktop_json_upsert() {
    local target="$1" py=""
    py="$(_maintenance_python)"
    if [[ -z "$py" ]]; then
        warn "Не нашёл Python для правки $target, пропускаю."
        return 1
    fi

    [[ -f "$target" ]] && cp "$target" "$target.bak-$(date +%Y%m%d%H%M%S)"

    "$py" - "$target" "$VENV_DIR/bin/telegram-mcp-ag" "$SERVER_NAME" <<'PYEOF'
import json
import sys

path, command, server_name = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}

data.setdefault("mcpServers", {})[server_name] = {"command": command, "args": []}

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF
}

register_claude_desktop() {
    local dir="" target="" found=""
    while IFS= read -r dir; do
        if [[ -d "$dir" ]]; then
            found="$dir"
            break
        fi
    done < <(_claude_desktop_candidates)

    [[ -z "$found" ]] && return 0

    target="$found/claude_desktop_config.json"
    info "Обнаружен Claude Desktop -- регистрирую сервер..."
    if _claude_desktop_json_upsert "$target"; then
        ok "Claude Desktop настроен ($target). Перезапустите приложение, чтобы изменения применились."
    else
        warn "Claude Desktop не настроен автоматически. Добавьте сервер вручную по примеру examples/claude-code.mcp.json."
    fi
}

# ---------------------------------------------------------------------------
# Self-check + summary
# ---------------------------------------------------------------------------

self_check() {
    echo
    info "Проверяю подключение к Telegram..."
    local output="" status=0
    output="$("$VENV_DIR/bin/python" - <<'PYEOF' 2>&1
import asyncio
import json
import sys

from telegram_mcp_ag.server import check_transcription_access

result = asyncio.run(check_transcription_access())
try:
    data = json.loads(result)
except json.JSONDecodeError:
    data = None

print(result)
sys.exit(0 if data and "is_premium" in data else 1)
PYEOF
    )" || status=$?

    if [[ "$status" -eq 0 ]]; then
        ok "Подключение к Telegram работает."
    else
        warn "Самопроверка не прошла:"
        echo "$output" >&2
        warn "config.env записан, но сервер пока не отвечает как ожидалось. Запустите вручную: $VENV_DIR/bin/telegram-mcp-ag"
    fi
}

print_summary() {
    local error_log=""
    error_log="$("$VENV_DIR/bin/python" -c '
import os
import telegram_mcp
package_dir = os.path.dirname(os.path.abspath(telegram_mcp.__file__))
print(os.path.join(os.path.dirname(package_dir), "mcp_errors.log"))
' 2>/dev/null || true)"

    echo
    ok "Готово! telegram-mcp-ag установлен в $INSTALL_DIR"
    echo
    echo "Что дальше:"
    echo "  - Перезапустите Claude Code / Codex / Claude Desktop, если они уже были открыты."
    echo "  - Проверить сервер вручную: $VENV_DIR/bin/telegram-mcp-ag"
    if [[ -n "$error_log" ]]; then
        echo "  - Лог ошибок сервера: $error_log"
    fi
    echo "  - Повторный вход в Telegram (например, после отзыва сессии): bash install.sh --relogin"
    echo "  - Полное удаление: bash install.sh --uninstall"
    echo
    echo "Секреты лежат в $CONFIG_PATH (права 600). Никому их не показывайте и не публикуйте."
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

_codex_toml_remove() {
    local codex_config="$1" py=""
    [[ -f "$codex_config" ]] || return 0
    py="$(_maintenance_python)"
    if [[ -z "$py" ]]; then
        warn "Не нашёл Python -- удалите вручную блок [mcp_servers.$SERVER_NAME] из $codex_config."
        return 0
    fi
    "$py" -m pip install -q tomlkit 2>/dev/null || {
        warn "Не удалось установить tomlkit -- удалите вручную блок [mcp_servers.$SERVER_NAME] из $codex_config."
        return 0
    }
    cp "$codex_config" "$codex_config.bak-$(date +%Y%m%d%H%M%S)"
    "$py" - "$codex_config" "$SERVER_NAME" <<'PYEOF'
import sys
import tomlkit

config_path, server_name = sys.argv[1], sys.argv[2]

with open(config_path, "r", encoding="utf-8") as f:
    doc = tomlkit.parse(f.read())

servers = doc.get("mcp_servers")
if servers is not None and server_name in servers:
    del servers[server_name]
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
PYEOF
    ok "Запись Codex удалена из $codex_config."
}

_claude_desktop_json_remove() {
    local target="$1" py=""
    [[ -f "$target" ]] || return 0
    py="$(_maintenance_python)"
    if [[ -z "$py" ]]; then
        warn "Не нашёл Python -- удалите вручную запись \"$SERVER_NAME\" из $target."
        return 0
    fi
    cp "$target" "$target.bak-$(date +%Y%m%d%H%M%S)"
    "$py" - "$target" "$SERVER_NAME" <<'PYEOF'
import json
import sys

path, server_name = sys.argv[1], sys.argv[2]

with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

servers = data.get("mcpServers")
if servers is not None and server_name in servers:
    del servers[server_name]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
PYEOF
    ok "Запись Claude Desktop удалена из $target."
}

do_uninstall() {
    info "Снимаю регистрации из клиентов..."

    if command -v claude >/dev/null 2>&1; then
        if claude mcp remove -s user "$SERVER_NAME" >/dev/null 2>&1; then
            ok "Claude Code: сервер удалён."
        fi
    fi

    if command -v codex >/dev/null 2>&1; then
        if codex mcp remove "$SERVER_NAME" >/dev/null 2>&1; then
            ok "Codex: сервер удалён."
        else
            _codex_toml_remove "$HOME/.codex/config.toml"
        fi
    else
        _codex_toml_remove "$HOME/.codex/config.toml"
    fi

    local dir=""
    while IFS= read -r dir; do
        _claude_desktop_json_remove "$dir/claude_desktop_config.json"
    done < <(_claude_desktop_candidates)

    if [[ -d "$INSTALL_DIR" ]]; then
        if confirm "Удалить папку $INSTALL_DIR вместе с сохранённой сессией?"; then
            rm -rf "$INSTALL_DIR"
            ok "Папка $INSTALL_DIR удалена."
        else
            info "Папка $INSTALL_DIR оставлена без изменений."
        fi
    fi

    echo
    warn "Не забудьте отозвать сессию в самом Telegram: Settings -> Devices -> найдите устройство \"telegram-mcp-ag (...)\" и завершите его."
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

print_help() {
    cat <<EOF
Использование: install.sh [--relogin | --uninstall]

  (без флагов)  Установить или обновить telegram-mcp-ag.
  --relogin     Переустановить пакет и заново войти в Telegram,
                даже если config.env уже существует.
  --uninstall   Снять регистрации из клиентов и предложить удалить
                папку $INSTALL_DIR.
  -h, --help    Показать эту справку.
EOF
}

main() {
    local do_uninstall_flag=0
    local relogin_flag=0

    for arg in "$@"; do
        case "$arg" in
            --uninstall) do_uninstall_flag=1 ;;
            --relogin) relogin_flag=1 ;;
            -h | --help)
                print_help
                exit 0
                ;;
            *)
                error "Неизвестный флаг: $arg"
                print_help
                exit 1
                ;;
        esac
    done

    detect_os
    require_tty

    if [[ "$do_uninstall_flag" -eq 1 ]]; then
        do_uninstall
        exit 0
    fi

    setup_python_and_venv
    install_package

    if [[ "$relogin_flag" -eq 0 ]] && has_full_config "$CONFIG_PATH"; then
        info "Найден существующий $CONFIG_PATH -- использую его без повторного входа."
        info "Чтобы войти заново (например, после отзыва сессии), запустите: bash install.sh --relogin"
    else
        prompt_api_credentials
        run_telegram_login
        write_config_env
    fi

    register_claude_code
    register_codex
    register_claude_desktop
    self_check
    print_summary
}

if [[ "${BASH_SOURCE[0]:-}" == "${0}" ]]; then
    main "$@"
fi
