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
# setup_python_and_venv() renames $VENV_DIR here before rebuilding it fresh
# in place (not in a staging dir moved into place afterward -- venvs bake an
# absolute shebang to their own creation path, so renaming a *completed*
# venv directory breaks every console-script entry point inside it with
# "bad interpreter"). See _restore_venv_backup_on_failure/_confirm_new_venv.
VENV_BACKUP_DIR="$VENV_DIR.bak"
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

# Set by prompt_auto_approve(), read by register_claude_code()/register_codex().
AUTO_APPROVE_TOOLS=0

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

# If setup_python_and_venv() got as far as renaming the old, working venv
# aside but install_package() (or anything after it) then failed, this puts
# it back. Runs on every exit -- whether from `set -e` aborting or a normal
# return -- so a mid-install failure never leaves the user with neither a
# working old venv nor a finished new one. _confirm_new_venv() deletes the
# backup once the new venv is proven to work, so by then this is a no-op.
_restore_venv_backup_on_failure() {
    if [[ -d "$VENV_BACKUP_DIR" ]]; then
        rm -rf "$VENV_DIR"
        mv "$VENV_BACKUP_DIR" "$VENV_DIR"
    fi
    return 0
}
trap '_restore_venv_backup_on_failure; _cleanup_maintenance_venv' EXIT

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
# Python / venv setup (uv is an accelerator, not a requirement -- system
# Python is always preferred when it already satisfies the version floor).
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

    # Rename the old venv aside rather than deleting it: if install_package()
    # below fails (network hiccup, most commonly), the EXIT trap
    # (_restore_venv_backup_on_failure) puts it right back, so a working
    # install is never left half-destroyed. Built fresh at $VENV_DIR itself,
    # not a staging path swapped in afterward -- see VENV_BACKUP_DIR's
    # definition for why that specifically doesn't work for venvs.
    rm -rf "$VENV_BACKUP_DIR"
    if [[ -d "$VENV_DIR" ]]; then
        mv "$VENV_DIR" "$VENV_BACKUP_DIR"
    fi
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

# The venv rebuild is confirmed good at this point (setup_python_and_venv()
# and install_package() both already succeeded) -- the backup is no longer
# needed, and dropping it here is what makes _restore_venv_backup_on_failure
# a no-op on a successful run instead of undoing a perfectly good install.
_confirm_new_venv() {
    rm -rf "$VENV_BACKUP_DIR"
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

prompt_auto_approve() {
    echo
    info "Сервер умеет только читать Telegram -- отправка сообщений и другие"
    info "изменяющие действия физически недоступны, независимо от ответа ниже."
    if confirm "Разрешать инструменты чтения автоматически, без подтверждения в чате на каждый вызов?"; then
        AUTO_APPROVE_TOOLS=1
    else
        AUTO_APPROVE_TOOLS=0
        info "Ассистент будет спрашивать подтверждение на каждый вызов -- это можно изменить позже в настройках клиента."
    fi
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
        if [[ "$AUTO_APPROVE_TOOLS" -eq 1 ]]; then
            _claude_settings_permission_set "$HOME/.claude/settings.json" add
        fi
    else
        warn "Не удалось зарегистрировать сервер в Claude Code. Добавьте вручную: claude mcp add -s user $SERVER_NAME -- $VENV_DIR/bin/telegram-mcp-ag"
    fi
}

# Adds or removes "mcp__$SERVER_NAME" in the top-level permissions.allow array
# of a Claude Code settings.json (user-scope by default -- matches the
# `-s user` registration above). $2 is "add" or "remove"; safe to call
# repeatedly either way.
_claude_settings_permission_set() {
    local settings_path="$1" action="$2" py=""
    py="$(_maintenance_python)"
    if [[ -z "$py" ]]; then
        warn "Не нашёл Python для правки $settings_path, пропускаю."
        return 1
    fi

    mkdir -p "$(dirname "$settings_path")"
    [[ -f "$settings_path" ]] && cp "$settings_path" "$settings_path.bak-$(date +%Y%m%d%H%M%S)"

    "$py" - "$settings_path" "mcp__$SERVER_NAME" "$action" <<'PYEOF'
import json
import sys

path, rule, action = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}

allow = data.setdefault("permissions", {}).setdefault("allow", [])
if action == "add":
    if rule not in allow:
        allow.append(rule)
else:
    data["permissions"]["allow"] = [r for r in allow if r != rule]

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, ensure_ascii=False)
    f.write("\n")
PYEOF
    if [[ "$action" == "add" ]]; then
        ok "Claude Code: разрешение на инструменты чтения добавлено ($settings_path)."
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

# `codex mcp add` has no flag for this, so it's always a follow-up TOML edit,
# whichever way the server entry itself got created. Requires the
# [mcp_servers.$SERVER_NAME] table to already exist.
_codex_toml_set_approval() {
    local codex_config="$1" py=""
    py="$(_maintenance_python)"
    if [[ -z "$py" ]]; then
        warn "Не нашёл Python для правки $codex_config, пропускаю разрешение автозапуска."
        return 1
    fi
    "$py" -m pip install -q tomlkit || {
        warn "Не удалось установить tomlkit, пропускаю разрешение автозапуска."
        return 1
    }

    "$py" - "$codex_config" "$SERVER_NAME" <<'PYEOF'
import sys
import tomlkit

config_path, server_name = sys.argv[1], sys.argv[2]

with open(config_path, "r", encoding="utf-8") as f:
    doc = tomlkit.parse(f.read())

servers = doc.get("mcp_servers")
if servers is not None and server_name in servers:
    servers[server_name]["default_tools_approval_mode"] = "approve"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(tomlkit.dumps(doc))
PYEOF
    ok "Codex: разрешение на инструменты чтения добавлено ($codex_config)."
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
            if [[ "$AUTO_APPROVE_TOOLS" -eq 1 ]]; then
                _codex_toml_set_approval "$codex_config"
            fi
            return 0
        fi
        warn "'codex mcp add' не сработал (команда экспериментальная), правлю $codex_config напрямую."
    fi

    if _codex_toml_upsert "$codex_config"; then
        [[ "$AUTO_APPROVE_TOOLS" -eq 1 ]] && _codex_toml_set_approval "$codex_config"
    else
        warn "Codex/ChatGPT Desktop не настроены автоматически. Смотрите examples/codex.config.toml."
    fi
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
        if [[ "$AUTO_APPROVE_TOOLS" -eq 1 ]]; then
            info "У Claude Desktop нет способа разрешить это заранее -- при первом вызове инструмента нажмите \"Always Allow\"."
        fi
    else
        warn "Claude Desktop не настроен автоматически. Добавьте сервер вручную по примеру examples/claude-code.mcp.json."
    fi
}

# Claude Code (and the local/SSH/"Code" sessions of the Claude Code Desktop
# app) reads personal skills from ~/.claude/skills/ -- copying ours there
# (from the same ref the package itself was installed from) is what makes
# "сделай сводку" work as a skill in *any* project on this machine, not just
# when working inside a checkout of this repo. Ordinary Claude Desktop chat
# does NOT read this directory: it loads skills synced from the user's
# claude.ai account (Settings -> Customize), which no installer script can
# configure -- see the note printed below when Desktop is detected.
#
# Best-effort by design, and isolated in its own subshell with -e/-u turned
# off: a flaky download or a locale/bash-version quirk in here must never be
# able to abort the rest of the install. This isn't hypothetical -- bash 3.2
# (macOS's frozen system bash) has thrown a bogus "unbound variable" on this
# exact code under a non-ASCII locale, killing an otherwise-successful
# install right at the finish line.
_install_claude_skill() {
    local name="$1" dest="$HOME/.claude/skills/$1"
    mkdir -p "$dest"
    if curl -fsSL "https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/$REPO_REF/.claude/skills/$name/SKILL.md" \
        -o "$dest/SKILL.md" 2>/dev/null; then
        ok "Скилл $name установлен ($dest)."
    else
        warn "Не удалось скачать скилл $name -- не критично, остальное работает и без него."
        rm -rf "$dest"
    fi
}

# claude.ai's "Upload a skill" dialog wants a zip with a top-level
# telegram-digest/ folder, not a bare SKILL.md -- this is the exact same
# file the README links to for readers who add the skill by hand, so both
# routes end up with byte-identical output, and the user never has to
# archive anything themselves.
_install_claude_skill_zip() {
    if curl -fsSL "https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/$REPO_REF/.claude/skills/telegram-digest.zip" \
        -o "$INSTALL_DIR/telegram-digest-skill.zip" 2>/dev/null; then
        # Also copy to Downloads -- that's where README tells users to look for
        # it, since it's a folder every non-developer already knows how to find
        # (unlike $INSTALL_DIR). mkdir -p/cp -f so an existing file from a
        # previous run is silently replaced, never an error.
        mkdir -p "$HOME/Downloads" 2>/dev/null
        cp -f "$INSTALL_DIR/telegram-digest-skill.zip" "$HOME/Downloads/telegram-digest-skill.zip" 2>/dev/null || true
        ok "Готовый архив скилла для Claude Desktop (в Загрузках): $HOME/Downloads/telegram-digest-skill.zip"
    else
        warn "Не удалось скачать архив скилла для Claude Desktop -- не критично, остальное работает и без него."
        rm -f "$INSTALL_DIR/telegram-digest-skill.zip"
    fi
}

install_claude_skills() {
    local have_cli=0 have_desktop=0 dir=""
    command -v claude >/dev/null 2>&1 && have_cli=1
    while IFS= read -r dir; do
        [[ -d "$dir" ]] && have_desktop=1
    done < <(_claude_desktop_candidates)
    [[ "$have_cli" -eq 0 && "$have_desktop" -eq 0 ]] && return 0

    info "Устанавливаю скилл Claude (сводка по Telegram)..."
    ( set +eu; _install_claude_skill "telegram-digest" ) \
        || warn "Установка скилла не удалась -- не критично, остальное работает и без него."

    if [[ "$have_desktop" -eq 1 ]]; then
        ( set +eu; _install_claude_skill_zip ) \
            || warn "Не удалось подготовить архив скилла -- не критично, остальное работает и без него."
        info "В обычном чате Claude Desktop скиллы читаются не с диска, а из вашего аккаунта claude.ai: чтобы сводка работала и там, подключите файл telegram-digest-skill.zip из Загрузок через значок профиля -> Settings -> Customize -> Skills -> Add -> Upload a skill (подробности в README)."
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
        _claude_settings_permission_set "$HOME/.claude/settings.json" remove
    fi

    if [[ -d "$HOME/.claude/skills/telegram-digest" || -d "$HOME/.claude/skills/setup-telegram-mcp" ]]; then
        rm -rf "$HOME/.claude/skills/telegram-digest" "$HOME/.claude/skills/setup-telegram-mcp"
        ok "Скиллы Claude удалены."
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
  --relogin     Войти в Telegram заново (например, если сессия отозвана
                вручную в Settings -> Devices) -- даже если config.env
                уже существует.
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
    _confirm_new_venv

    if [[ "$relogin_flag" -eq 0 ]] && has_full_config "$CONFIG_PATH"; then
        info "Найден существующий $CONFIG_PATH -- использую его без повторного входа."
        info "Чтобы войти заново (например, после отзыва сессии), запустите: bash install.sh --relogin"
    else
        prompt_api_credentials
        run_telegram_login
        write_config_env
    fi

    prompt_auto_approve
    register_claude_code
    register_codex
    register_claude_desktop
    install_claude_skills
    self_check
    print_summary
}

# `${BASH_SOURCE[0]} == ${0}` looks equivalent but isn't: when this script
# runs via `curl ... | bash` there is no source file at all, so BASH_SOURCE[0]
# is empty while $0 is "bash" -- the comparison is always false and main()
# silently never runs. `return` only succeeds inside a sourced script/function,
# so this works the same whether the script came from a file, stdin, or a pipe.
if ! (return 0 2>/dev/null); then
    main "$@"
fi
