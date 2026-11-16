#!/usr/bin/env bash
# Installer for telegram-mcp-ag (macOS / Linux).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.sh | bash
#   bash install.sh --relogin     # redo the Telegram login, keep everything else
#   bash install.sh --relogin --account work   # the same for one of several accounts
#   bash install.sh --add-account # sign in a second (third...) Telegram account
#   bash install.sh --uninstall   # remove client registrations and (optionally) the install dir
#
# All user-facing text is Russian: this installer targets people who have
# never opened a terminal before (see CLAUDE.md). Code comments are English.
set -euo pipefail

REPO_ARCHIVE_BASE="https://github.com/agroznykh/telegram-mcp-ag/archive"
REPO_RAW_BASE="https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag"
UPSTREAM_ARCHIVE_BASE="https://github.com/chigwell/telegram-mcp/archive"
# The server gets its own uv-managed Python, not one that belongs to another
# program: a Homebrew cleanup or similar would take the venv down with it. Change
# this only after the new version passes CI and a live QR login.
PYTHON_VERSION="3.14"
REPO_API="https://api.github.com/repos/agroznykh/telegram-mcp-ag/releases/latest"
# Override for testing: TELEGRAM_MCP_AG_REF=some-branch bash install.sh
# Resolved below, once the output helpers it might warn through are defined.
REPO_REF=""

# Must match telegram_mcp_ag.config.CONFIG_DIR exactly -- it is not
# configurable, so this path cannot be overridden here either.
INSTALL_DIR="$HOME/telegram-mcp-ag"
VENV_DIR="$INSTALL_DIR/.venv"
# Where the installer reads the person's answers from (it normally runs from a
# pipe, so stdin is the script itself). A variable so that tests can point it
# elsewhere.
TTY_DEVICE="/dev/tty"
# setup_python_and_venv() renames $VENV_DIR here before rebuilding it fresh
# in place (not in a staging dir moved into place afterward -- venvs bake an
# absolute shebang to their own creation path, so renaming a *completed*
# venv directory breaks every console-script entry point inside it with
# "bad interpreter"). See _restore_venv_backup_on_failure/_confirm_new_venv.
VENV_BACKUP_DIR="$VENV_DIR.bak"
# See _fetch_upstream(). UPSTREAM_SOURCE stays empty on the pip fallback path.
UPSTREAM_BASE_DIR="$INSTALL_DIR/upstream"
UPSTREAM_SOURCE=""
# Filled by _ref_pyproject() and install_package(); see there.
REF_PYPROJECT=""
INSTALLED_VERSION=""
CONFIG_PATH="$INSTALL_DIR/config.env"
SERVER_NAME="telegram-mcp-ag"

# The command a person can paste to run this installer again with a flag. The
# installer normally arrives through a pipe (curl | bash), so there is no
# install.sh in the current folder to point them at.
installer_command() {
    if [[ -z "${1:-}" ]]; then
        printf '%s' "curl -fsSL https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.sh | bash"
    else
        printf '%s' "curl -fsSL https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/main/install.sh | bash -s -- $1"
    fi
}

# Filled by parse_args(): install (no flags), add-account, relogin or uninstall,
# and the account label given with --account (empty if none).
ACTION="install"
ACCOUNT_ARG=""
# Filled by load_accounts(): canonical labels, one per line, and the label of the
# default account (empty if none). Labels come only from telegram-mcp-ag-login
# --list-accounts: config.env is never parsed here.
ACCOUNT_LABELS=""
ACCOUNT_DEFAULT=""
# The account the final self-check asks about; empty means "no account argument".
SELF_CHECK_ACCOUNT=""

# Filled in by prompt_api_credentials() before write_config_env() reads
# them. Pre-declared (empty) so `set -u` doesn't complain if that ever changes.
API_ID=""
API_HASH=""
OS=""
ARCH=""

# Read by register_claude_code()/register_codex(). Always on, without asking:
# the server is read-only, so a confirmation on every call protects nothing.
AUTO_APPROVE_TOOLS=1

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
    read -r -p "$1" reply <"$TTY_DEVICE"
    printf -v "$2" '%s' "$reply"
}

confirm() {
    local reply=""
    read -r -p "$1 [y/N] " reply <"$TTY_DEVICE" || true
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
    if [[ ! -r "$TTY_DEVICE" ]]; then
        error "Нужен интерактивный терминал: установщик спрашивает api_id/api_hash и просит войти в Telegram."
        error "Скачайте скрипт и запустите его напрямую: curl -fsSL <ссылка на install.sh> -o install.sh && bash install.sh"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Python / venv setup. uv provides its own $PYTHON_VERSION, reusing a copy it
# already has; whatever Python the machine has is not used.
# ---------------------------------------------------------------------------

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
    if ! command -v uv >/dev/null 2>&1; then
        install_uv
    fi

    if ! command -v uv >/dev/null 2>&1; then
        error "Не удалось установить uv. Проверьте интернет и запустите установщик ещё раз."
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
    info "Создаю виртуальное окружение на Python $PYTHON_VERSION (если его ещё нет, он скачается)..."
    # --seed: the client-config helpers run "$py" -m pip install tomlkit.
    if ! uv venv --seed --managed-python --python "$PYTHON_VERSION" "$VENV_DIR" >/dev/null; then
        error "Не удалось скачать Python $PYTHON_VERSION. Проверьте интернет и запустите установщик ещё раз."
        exit 1
    fi
    ok "Окружение готово: $VENV_DIR"
}

# Downloads the pinned upstream source, so installing needs no git. An archive
# URL won't do: upstream's install_guard accepts only a git or file:// install,
# and on every start checks that the source dir is still there -- which is why
# it lives in $INSTALL_DIR rather than a temp dir. The SHA comes from the
# release's own pyproject.toml, the one place it is pinned.
_fetch_upstream() {
    local sha=""
    _ref_pyproject
    sha="$(printf '%s\n' "$REF_PYPROJECT" | grep -oE 'chigwell/telegram-mcp\.git@[0-9a-f]{40}' | head -1 | cut -d@ -f2)" || true
    if [[ -z "$sha" ]]; then
        error "Не удалось узнать, какую версию компонентов скачивать. Проверьте интернет и запустите установщик ещё раз."
        exit 1
    fi
    UPSTREAM_SOURCE="$UPSTREAM_BASE_DIR/$sha"
    [[ -f "$UPSTREAM_SOURCE/pyproject.toml" ]] && return 0

    info "Скачиваю компоненты сервера..."
    local partial="$UPSTREAM_SOURCE.partial"
    rm -rf "$partial"
    mkdir -p "$partial"
    if ! curl -fsSL "$UPSTREAM_ARCHIVE_BASE/$sha.tar.gz" | tar -xz --strip-components=1 -C "$partial"; then
        rm -rf "$partial"
        error "Не удалось скачать компоненты сервера. Проверьте интернет и запустите установщик ещё раз."
        exit 1
    fi
    rm -rf "$UPSTREAM_SOURCE"
    mv "$partial" "$UPSTREAM_SOURCE"
}

# pyproject.toml of $REPO_REF, downloaded once into REF_PYPROJECT: it gives both
# the version shown to the user and the upstream SHA. Called directly, not in
# $(...), so the variable survives. Empty if the download fails or stalls.
_ref_pyproject() {
    if [[ -z "$REF_PYPROJECT" ]]; then
        REF_PYPROJECT="$(curl -fsSL --max-time 30 "$REPO_RAW_BASE/$REPO_REF/pyproject.toml" 2>/dev/null)" || true
    fi
}

# Version of the package about to be installed; empty if it cannot be read
# (the install itself does not depend on it).
_ref_version() {
    printf '%s\n' "$REF_PYPROJECT" | sed -n 's/^version *= *"\([^"]*\)".*/\1/p' | head -1 || true
}

# Version of the package installed in the venv; empty if it cannot be read.
_installed_version() {
    "$VENV_DIR/bin/python" -c 'import telegram_mcp_ag; print(telegram_mcp_ag.__version__)' 2>/dev/null || true
}

install_package() {
    local version=""
    _ref_pyproject
    version="$(_ref_version)"
    info "Устанавливаю telegram-mcp-ag${version:+ $version} (ref: $REPO_REF)..."
    local package_url="$REPO_ARCHIVE_BASE/$REPO_REF.tar.gz"
    # Reinstalling: the upstream telegram-mcp is pinned by SHA but keeps the
    # same version string across SHAs, so a plain install on an existing venv
    # sees "already satisfied" and leaves the old upstream under new code.
    _fetch_upstream
    local overrides=""
    overrides="$(mktemp)"
    printf 'telegram-mcp @ %s\n' \
        "$("$VENV_DIR/bin/python" -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).as_uri())' "$UPSTREAM_SOURCE")" >"$overrides"
    uv pip install -q --python "$VENV_DIR/bin/python" --reinstall --overrides "$overrides" "$package_url"
    rm -f "$overrides"
    INSTALLED_VERSION="$(_installed_version)"
    ok "Пакет telegram-mcp-ag${INSTALLED_VERSION:+ $INSTALLED_VERSION} установлен."
}

# The venv rebuild is confirmed good at this point (setup_python_and_venv()
# and install_package() both already succeeded) -- the backup is no longer
# needed, and dropping it here is what makes _restore_venv_backup_on_failure
# a no-op on a successful run instead of undoing a perfectly good install.
_confirm_new_venv() {
    rm -rf "$VENV_BACKUP_DIR"
    # Older upstream copies had to survive until now: the backup venv needed them.
    if [[ -n "$UPSTREAM_SOURCE" ]]; then
        find "$UPSTREAM_BASE_DIR" -mindepth 1 -maxdepth 1 ! -name "$(basename "$UPSTREAM_SOURCE")" -exec rm -rf {} +
    fi
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

# One run of the login command, for the account with this label (none: the
# command picks the only one itself). A function so that tests can stand in for
# the command; the person's terminal is its stdin because the installer's own
# stdin is the script when it arrives through a pipe.
_run_login_once() {
    local label="${1:-}"
    if [[ -n "$label" ]]; then
        PYTHONUNBUFFERED=1 TELEGRAM_API_ID="$API_ID" TELEGRAM_API_HASH="$API_HASH" \
            "$VENV_DIR/bin/telegram-mcp-ag-login" --account "$label" <"$TTY_DEVICE"
    else
        PYTHONUNBUFFERED=1 TELEGRAM_API_ID="$API_ID" TELEGRAM_API_HASH="$API_HASH" \
            "$VENV_DIR/bin/telegram-mcp-ag-login" <"$TTY_DEVICE"
    fi
}

# $1 = label of the account to sign in (empty: the only one, or the first).
run_telegram_login() {
    local label="${1:-}"
    echo
    if [[ -n "$label" ]]; then
        info "Вход в аккаунт \"$label\". В приложении Telegram должен быть сейчас открыт именно он."
    fi
    info "Вход в Telegram: сейчас появится QR-код."
    info "В приложении Telegram: Настройки -> Устройства -> Подключить устройство -> отсканировать QR-код."
    echo

    # The login command merges the session into config.env, and falls back to
    # a phone-number login on its own if the QR code is never scanned. It
    # prints the session nowhere. Exit codes: 3 -- Telegram rejected
    # api_id/api_hash; 4 -- several accounts and none named; 5 -- the person
    # said it is the wrong account (nothing was saved).
    local status=0
    while true; do
        status=0
        _run_login_once "$label" || status=$?
        if [[ "$status" -eq 0 ]]; then
            return 0
        fi
        if [[ "$status" -eq 3 ]]; then
            prompt_api_credentials
            save_api_credentials
            continue
        fi
        if [[ "$status" -eq 4 ]]; then
            load_accounts || true
            choose_account "В какой аккаунт входить?"
            label="$CHOSEN_ACCOUNT"
            continue
        fi
        if [[ "$status" -eq 5 ]]; then
            info "Хорошо, ничего не сохранено."
            if confirm "Попробовать войти ещё раз?"; then
                continue
            fi
        fi
        local retry="--relogin"
        if [[ -n "$label" ]]; then
            retry="--relogin --account $label"
        fi
        error "Не удалось войти в Telegram. Запустите вход ещё раз: $(installer_command "$retry")"
        exit 1
    done
}

# api_id/api_hash don't change on a relogin -- they belong to the app on
# my.telegram.org, not to the session -- so a config.env that already has
# them (a relogin, or a session the server cleared after Telegram revoked
# it) is reused without asking again.
load_api_credentials() {
    local f="$1"
    [[ -f "$f" ]] || return 1
    local id="" hash=""
    id="$(grep -m1 -E '^TELEGRAM_API_ID=' "$f" | cut -d= -f2- || true)"
    hash="$(grep -m1 -E '^TELEGRAM_API_HASH=' "$f" | cut -d= -f2- || true)"
    id="${id%\"}"
    id="${id#\"}"
    hash="${hash%\"}"
    hash="${hash#\"}"
    [[ -n "$id" && -n "$hash" ]] || return 1
    API_ID="$id"
    API_HASH="$hash"
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
            printf 'TELEGRAM_EXPOSED_TOOLS=read-only\n'
            printf 'TELEGRAM_DEVICE_MODEL=%s\n' "$device_model"
        } >"$CONFIG_PATH"
    )
    chmod 600 "$CONFIG_PATH"
    ok "api_id и api_hash сохранены."
}

has_session_keys() {
    local f="$1"
    [[ -f "$f" ]] || return 1
    grep -qE '^TELEGRAM_SESSION_(STRING|NAME)[A-Za-z0-9_]*=.+' "$f"
}

# After Telegram rejects api_id/api_hash: when config.env already holds sessions
# only the two values are replaced, in place (write_values keeps every other
# line and, on Windows, the file's ACL). write_config_env writes the file from
# scratch and would erase the accounts' sessions. The values travel in the
# environment, not in argv, where `ps` would show them.
save_api_credentials() {
    if has_session_keys "$CONFIG_PATH"; then
        TELEGRAM_API_ID="$API_ID" TELEGRAM_API_HASH="$API_HASH" "$VENV_DIR/bin/python" -c '
import os
from telegram_mcp_ag import config

config.write_values(
    {"TELEGRAM_API_ID": os.environ["TELEGRAM_API_ID"], "TELEGRAM_API_HASH": os.environ["TELEGRAM_API_HASH"]},
    config.CONFIG_PATH,
)
'
        ok "api_id и api_hash обновлены (сохранённые сессии не тронуты)."
    else
        write_config_env
    fi
}

# ---------------------------------------------------------------------------
# Accounts. The installer never reads config.env for them: the labels come from
# `telegram-mcp-ag-login --list-accounts`, the changes go through its options.
# ---------------------------------------------------------------------------

# Whether the installed telegram-mcp-ag-login knows --account. The installer is
# downloaded from `main` but the package comes from the latest release, so right
# after the multi-account code lands in main the package may still be older.
# The help text is captured first: grep -q would close the pipe early and
# pipefail would turn that into a failure.
login_supports_accounts() {
    local help_text=""
    help_text="$("$VENV_DIR/bin/telegram-mcp-ag-login" --help 2>&1)" || true
    [[ "$help_text" == *"--account"* ]]
}

require_accounts_support() {
    if login_supports_accounts; then
        return 0
    fi
    error "$1 появится в следующей версии: установлен пакет, который ещё не умеет работать с несколькими аккаунтами. Ничего не изменено."
    exit 1
}

# Fills ACCOUNT_LABELS and ACCOUNT_DEFAULT. A line is "метка" or "метка (по
# умолчанию)": only the first word is used, and only if it is a canonical label,
# since it ends up in commands the person will paste.
load_accounts() {
    local output="" line="" label=""
    ACCOUNT_LABELS=""
    ACCOUNT_DEFAULT=""
    output="$("$VENV_DIR/bin/telegram-mcp-ag-login" --list-accounts 2>/dev/null)" || return 1
    while IFS= read -r line; do
        label="${line%% *}"
        [[ "$label" =~ ^[a-z0-9_]{1,32}$ ]] || continue
        ACCOUNT_LABELS="${ACCOUNT_LABELS:+$ACCOUNT_LABELS$'\n'}$label"
        if [[ "$line" == *" ("* ]]; then
            ACCOUNT_DEFAULT="$label"
        fi
    done <<<"$output"
    return 0
}

# Number of lines in $1 (a newline-separated list), 0 for an empty one.
_count_lines() {
    local n=0 line=""
    while IFS= read -r line; do
        [[ -n "$line" ]] && n=$((n + 1))
    done <<<"$1"
    printf '%s' "$n"
}

_in_lines() {
    local needle="$1" line=""
    while IFS= read -r line; do
        [[ "$line" == "$needle" ]] && return 0
    done <<<"$2"
    return 1
}

_join_lines() {
    local out="" line=""
    while IFS= read -r line; do
        [[ -n "$line" ]] && out="${out:+$out, }$line"
    done <<<"$1"
    printf '%s' "$out"
}

# "метка1 (по умолчанию), метка2" for messages.
_describe_accounts() {
    local out="" line=""
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        if [[ "$line" == "$ACCOUNT_DEFAULT" ]]; then
            line="$line (по умолчанию)"
        fi
        out="${out:+$out, }$line"
    done <<<"$ACCOUNT_LABELS"
    printf '%s' "$out"
}

# Prints the canonical form of a label, or the reason it is not allowed (on
# stderr, in Russian) and fails. $2 = 1 to let "default" through. The check is
# the package's own, so the installer and the server cannot disagree.
_validated_label() {
    "$VENV_DIR/bin/python" -c '
import sys
from telegram_mcp_ag import config

try:
    print(config.validate_label(sys.argv[1], allow_default=sys.argv[2] == "1"))
except config.ConfigError as exc:
    print(exc, file=sys.stderr)
    sys.exit(1)
' "$1" "$2"
}

# $1 = question. Shows ACCOUNT_LABELS as a numbered list; fills CHOSEN_ACCOUNT.
CHOSEN_ACCOUNT=""
choose_account() {
    local reply="" label="" i=0 n=0
    n="$(_count_lines "$ACCOUNT_LABELS")"
    if [[ "$n" -eq 0 ]]; then
        error "Не нашёл сохранённых аккаунтов. Сначала выполните установку без флагов: $(installer_command "")"
        exit 1
    fi
    while true; do
        echo
        info "$1"
        i=0
        while IFS= read -r label; do
            [[ -n "$label" ]] || continue
            i=$((i + 1))
            echo "  $i) $label"
        done <<<"$ACCOUNT_LABELS"
        ask "Введите номер: " reply
        if [[ "$reply" =~ ^[0-9]+$ && "$reply" -ge 1 && "$reply" -le "$n" ]]; then
            i=0
            while IFS= read -r label; do
                [[ -n "$label" ]] || continue
                i=$((i + 1))
                if [[ "$i" -eq "$reply" ]]; then
                    CHOSEN_ACCOUNT="$label"
                    return 0
                fi
            done <<<"$ACCOUNT_LABELS"
        fi
        warn "Нужно ввести номер из списка."
    done
}

# Asks for a label until it is allowed and free. $1 = question, $2 = labels that
# may not be reused (newline-separated), $3 = 1 if empty input is an answer
# ("keep as it is"). Fills ASKED_LABEL (empty if skipped).
ASKED_LABEL=""
ask_new_label() {
    local reply="" label=""
    while true; do
        ask "$1" reply
        if [[ -z "$reply" ]]; then
            if [[ "${3:-0}" -eq 1 ]]; then
                ASKED_LABEL=""
                return 0
            fi
            warn "Название нужно: одно слово латиницей, например work."
            continue
        fi
        label="$(_validated_label "$reply" 0)" || continue
        if _in_lines "$label" "$2"; then
            warn "Название \"$label\" уже занято, выберите другое."
            continue
        fi
        ASKED_LABEL="$label"
        return 0
    done
}

# api_id/api_hash for a login: the saved ones, or ask and save.
ensure_api_credentials() {
    if load_api_credentials "$CONFIG_PATH"; then
        info "Использую сохранённые api_id и api_hash."
    else
        prompt_api_credentials
        save_api_credentials
    fi
}

# The accounts after the new one is added, as a newline-separated list: the
# existing ones (the unnamed one under its new name, if it gets one) and the new
# one. $1 = new label, $2 = new name of the unnamed account (may be empty).
_labels_after_add() {
    local label="" out=""
    while IFS= read -r label; do
        [[ -n "$label" ]] || continue
        if [[ "$label" == "default" && -n "$2" ]]; then
            label="$2"
        fi
        out="${out:+$out$'\n'}$label"
    done <<<"$ACCOUNT_LABELS"
    printf '%s' "${out:+$out$'\n'}$1"
}

# Which account answers when the chat does not name one. $1 = the accounts to
# pick from; $2 = the one that is chosen now (Enter keeps it; empty: none).
# Fills CHOSEN_DEFAULT (empty: "always ask").
CHOSEN_DEFAULT=""
choose_default_account() {
    local labels="$1" current="$2" reply="" label="" i=0 n=0
    n="$(_count_lines "$labels")"
    while true; do
        echo
        info "Какой аккаунт использовать, когда вы не говорите, какой именно?"
        i=0
        while IFS= read -r label; do
            [[ -n "$label" ]] || continue
            i=$((i + 1))
            if [[ "$label" == "$current" ]]; then
                echo "  $i) $label (сейчас)"
            else
                echo "  $i) $label"
            fi
        done <<<"$labels"
        echo "  0) никакой -- каждый раз спрашивать, какой нужен"
        if [[ -n "$current" ]]; then
            ask "Введите номер (Enter -- $current, как сейчас): " reply
        else
            ask "Введите номер (Enter -- никакой): " reply
        fi
        if [[ -z "$reply" ]]; then
            CHOSEN_DEFAULT="$current"
            return 0
        fi
        if [[ "$reply" == "0" ]]; then
            CHOSEN_DEFAULT=""
            return 0
        fi
        if [[ "$reply" =~ ^[0-9]+$ && "$reply" -ge 1 && "$reply" -le "$n" ]]; then
            i=0
            while IFS= read -r label; do
                [[ -n "$label" ]] || continue
                i=$((i + 1))
                if [[ "$i" -eq "$reply" ]]; then
                    CHOSEN_DEFAULT="$label"
                    return 0
                fi
            done <<<"$labels"
        fi
        warn "Нужно ввести номер из списка."
    done
}

add_account_flow() {
    require_accounts_support "Добавление аккаунта"
    if ! has_full_config "$CONFIG_PATH" || ! load_accounts || [[ -z "$ACCOUNT_LABELS" ]]; then
        error "Первого аккаунта ещё нет. Сначала выполните обычную установку: $(installer_command "")"
        exit 1
    fi

    echo
    info "Сейчас подключено: $(_describe_accounts)."
    info "Новому аккаунту нужно название -- по нему вы будете выбирать его в чате. Одно слово латиницей: например, work для рабочего или personal для личного."
    local new_label="" first_name="" current_default="$ACCOUNT_DEFAULT"
    ask_new_label "Название нового аккаунта: " "$ACCOUNT_LABELS"$'\n'"default" 0
    new_label="$ASKED_LABEL"

    if _in_lines "default" "$ACCOUNT_LABELS"; then
        echo
        info "Первый аккаунт пока без названия. Лучше его назвать, чтобы в чате различать оба."
        ask_new_label "Название первого аккаунта, например personal (Enter -- оставить без названия): " "$ACCOUNT_LABELS"$'\n'"$new_label" 1
        first_name="$ASKED_LABEL"
    fi

    local after="" renamed_default="$current_default"
    after="$(_labels_after_add "$new_label" "$first_name")"
    if [[ "$current_default" == "default" && -n "$first_name" ]]; then
        renamed_default="$first_name"
    fi
    choose_default_account "$after" "$renamed_default"

    ensure_api_credentials
    # The login goes first: if it fails or is declined, nothing has changed yet.
    run_telegram_login "$new_label"

    # What the default is now, so that it is rewritten only if the choice differs.
    local now_default="$current_default"
    if [[ -n "$first_name" ]]; then
        if "$VENV_DIR/bin/telegram-mcp-ag-login" --rename-default "$first_name"; then
            now_default="$renamed_default"
        else
            warn "Не удалось дать первому аккаунту название \"$first_name\": он остался без названия. Аккаунт \"$new_label\" добавлен."
            if [[ "$CHOSEN_DEFAULT" == "$first_name" ]]; then
                CHOSEN_DEFAULT="default"
            fi
        fi
    fi
    if [[ "$CHOSEN_DEFAULT" != "$now_default" ]]; then
        if ! "$VENV_DIR/bin/telegram-mcp-ag-login" --set-default "${CHOSEN_DEFAULT:-none}"; then
            warn "Не удалось запомнить аккаунт по умолчанию. Повторите: $VENV_DIR/bin/telegram-mcp-ag-login --set-default ${CHOSEN_DEFAULT:-none}"
        fi
    fi

    SELF_CHECK_ACCOUNT="$new_label"
    echo
    ok "Аккаунт \"$new_label\" добавлен."
    warn "Сервер читает настройки при запуске: перезапустите Claude, Codex или ChatGPT, чтобы он увидел новый аккаунт."
}

relogin_flow() {
    local label=""
    if [[ -n "$ACCOUNT_ARG" ]]; then
        require_accounts_support "Перелогин по названию аккаунта (--account)"
        label="$(_validated_label "$ACCOUNT_ARG" 1)" || exit 1
        if ! load_accounts || ! _in_lines "$label" "$ACCOUNT_LABELS"; then
            error "Аккаунта \"$label\" нет${ACCOUNT_LABELS:+. Есть: $(_join_lines "$ACCOUNT_LABELS")}. Чтобы добавить новый, вставьте в терминал: $(installer_command --add-account)"
            exit 1
        fi
    elif login_supports_accounts && load_accounts && [[ "$(_count_lines "$ACCOUNT_LABELS")" -ge 2 ]]; then
        choose_account "Аккаунтов несколько. В какой войти заново?"
        label="$CHOSEN_ACCOUNT"
    fi

    ensure_api_credentials
    run_telegram_login "$label"
    SELF_CHECK_ACCOUNT="$label"
}

install_login_flow() {
    if has_full_config "$CONFIG_PATH"; then
        info "Вход в Telegram уже выполнен -- использую сохранённую сессию."
        if login_supports_accounts && load_accounts && [[ -n "$ACCOUNT_LABELS" ]]; then
            info "Аккаунты: $(_describe_accounts)."
            info "Добавить ещё один аккаунт, вставьте в терминал: $(installer_command --add-account)"
            if [[ "$(_count_lines "$ACCOUNT_LABELS")" -ge 2 ]]; then
                local first_label=""
                first_label="${ACCOUNT_LABELS%%$'\n'*}"
                info "Чтобы войти заново в один из них (например, после отзыва сессии), вставьте в терминал: $(installer_command "--relogin --account $first_label") -- вместо \"$first_label\" подставьте нужное название."
            else
                info "Чтобы войти заново (например, после отзыва сессии), вставьте в терминал: $(installer_command --relogin)"
            fi
        else
            info "Чтобы войти заново (например, после отзыва сессии), вставьте в терминал: $(installer_command --relogin)"
        fi
    else
        ensure_api_credentials
        run_telegram_login ""
    fi
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
    # was already removed), but we may still need to edit a client config,
    # so spin up a throwaway venv the same way the real one is built.
    command -v uv >/dev/null 2>&1 || return 0

    local venv_root=""
    venv_root="$(mktemp -d)"
    if ! uv venv --seed --managed-python --python "$PYTHON_VERSION" "$venv_root/venv" >/dev/null 2>&1; then
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
        ok "Claude Desktop настроен ($target). Чтобы он увидел сервер, выйдите из приложения полностью (на Mac -- Cmd+Q; просто закрыть окно мало) и откройте снова."
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
    # Second argument: the skills root. Codex CLI and the ChatGPT desktop app
    # read the same SKILL.md format from ~/.agents/skills, so the one file is
    # copied there too -- see install_codex_skills().
    local name="$1" dest="${2:-$HOME/.claude/skills}/$1"
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
# telegram-digest/ folder, not a bare SKILL.md -- the repo carries it ready
# built, named telegram-digest-skill-<version>.zip, so the user never has to
# archive anything themselves. It goes straight to Downloads, under the same
# name: that's where README tells users to look for it, since it's a folder
# every non-developer already knows how to find. The version in the name
# tells uploads of different skill versions apart.
_install_claude_skill_zip() {
    local base="https://raw.githubusercontent.com/agroznykh/telegram-mcp-ag/$REPO_REF/.claude/skills"
    local version="" name="telegram-digest-skill.zip"
    version="$(curl -fsSL "$base/telegram-digest/SKILL.md" 2>/dev/null |
        sed -nE 's/^[[:space:]]+version: "([0-9]+\.[0-9]+)"[[:space:]]*$/\1/p' | head -1)" || true
    [[ -n "$version" ]] && name="telegram-digest-skill-$version.zip"

    mkdir -p "$HOME/Downloads" 2>/dev/null
    local dest="$HOME/Downloads/$name"
    # Releases from before the zip carried its version in the repo have it
    # as telegram-digest.zip; the fallback can go once every release has the
    # new name.
    if curl -fsSL "$base/$name" -o "$dest" 2>/dev/null ||
        curl -fsSL "$base/telegram-digest.zip" -o "$dest" 2>/dev/null; then
        ok "Готовый архив скилла для Claude Desktop (в Загрузках): $dest"
        info "В обычном чате Claude Desktop скиллы читаются не с диска, а из вашего аккаунта claude.ai: чтобы сводка работала и там, подключите файл $name из Загрузок через значок профиля -> Settings -> Customize -> Skills -> Add -> Upload a skill (подробности в README)."
    else
        rm -f "$dest"
        warn "Не удалось скачать архив скилла для Claude Desktop -- не критично, остальное работает и без него."
    fi
    # Earlier installers kept a copy here too; nothing reads it.
    rm -f "$INSTALL_DIR/telegram-digest-skill.zip"
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
    fi
}

# Codex CLI, the Codex IDE extension and the ChatGPT desktop app read personal
# skills from ~/.agents/skills (the open agent-skills layout: a folder with a
# SKILL.md), picking up changes on their own. ChatGPT Chat/Work on the web and
# mobile only see skills shipped inside plugins, which is out of reach here.
# Same detection and the same best-effort subshell as the Claude skill above.
install_codex_skills() {
    if ! command -v codex >/dev/null 2>&1 && [[ ! -d "$HOME/.codex" ]]; then
        return 0
    fi

    info "Устанавливаю скилл для Codex / ChatGPT Desktop (сводка по Telegram)..."
    ( set +eu; _install_claude_skill "telegram-digest" "$HOME/.agents/skills" ) \
        || warn "Установка скилла не удалась -- не критично, остальное работает и без него."
}

# ---------------------------------------------------------------------------
# Self-check + summary
# ---------------------------------------------------------------------------

# The account the self-check asks about. With several accounts the server
# answers a call without `account` with a question instead of JSON, so one has
# to be named: the one just added or signed in again, else the default, else the
# first. With a single account (or an older package) none is named.
_self_check_account() {
    if [[ -n "$SELF_CHECK_ACCOUNT" ]]; then
        printf '%s' "$SELF_CHECK_ACCOUNT"
        return 0
    fi
    login_supports_accounts && load_accounts || return 0
    [[ "$(_count_lines "$ACCOUNT_LABELS")" -ge 2 ]] || return 0
    printf '%s' "${ACCOUNT_DEFAULT:-${ACCOUNT_LABELS%%$'\n'*}}"
}

self_check() {
    echo
    local account=""
    account="$(_self_check_account)"
    if [[ -n "$account" ]]; then
        info "Проверяю подключение к Telegram (аккаунт \"$account\")..."
    else
        info "Проверяю подключение к Telegram..."
    fi
    local output="" status=0
    output="$("$VENV_DIR/bin/python" - "$account" <<'PYEOF' 2>&1
import asyncio
import json
import sys

from telegram_mcp_ag.server import check_transcription_access

account = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None
result = asyncio.run(check_transcription_access(account=account))
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
        warn "Настройки сохранены, но сервер пока не отвечает как ожидалось. Запустите вручную: $VENV_DIR/bin/telegram-mcp-ag"
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
    ok "Готово! telegram-mcp-ag${INSTALLED_VERSION:+ $INSTALLED_VERSION} установлен."
    echo
    echo "Что дальше:"
    echo "  - Claude Desktop: выйдите полностью (на Mac -- Cmd+Q) и откройте снова."
    echo "  - Claude Code и Codex: откройте новую сессию."
    echo "  - ChatGPT Desktop: перезапускать обычно не нужно -- сервер и скилл подхватятся сами в новом чате."
    echo "  - Проверить сервер вручную: $VENV_DIR/bin/telegram-mcp-ag"
    if [[ -n "$error_log" ]]; then
        echo "  - Лог ошибок сервера: $error_log"
    fi
    if login_supports_accounts && load_accounts && [[ -n "$ACCOUNT_LABELS" ]]; then
        echo "  - Аккаунты: $(_describe_accounts)"
        echo "  - Добавить ещё один аккаунт, вставьте в терминал:"
        echo "      $(installer_command --add-account)"
        if [[ "$(_count_lines "$ACCOUNT_LABELS")" -ge 2 ]]; then
            local label=""
            echo "  - Повторный вход в Telegram (например, после отзыва сессии), вставьте в терминал команду нужного аккаунта:"
            while IFS= read -r label; do
                [[ -n "$label" ]] && echo "      $(installer_command "--relogin --account $label")"
            done <<<"$ACCOUNT_LABELS"
        else
            echo "  - Повторный вход в Telegram (например, после отзыва сессии), вставьте в терминал:"
            echo "      $(installer_command --relogin)"
        fi
    else
        echo "  - Повторный вход в Telegram (например, после отзыва сессии), вставьте в терминал:"
        echo "      $(installer_command --relogin)"
    fi
    echo "  - Полное удаление, вставьте в терминал:"
    echo "      $(installer_command --uninstall)"
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

    if [[ -d "$HOME/.agents/skills/telegram-digest" ]]; then
        rm -rf "$HOME/.agents/skills/telegram-digest"
        ok "Скилл Codex / ChatGPT Desktop удалён."
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
Использование: install.sh [--relogin [--account МЕТКА] | --add-account | --uninstall]

  (без флагов)    Установить или обновить telegram-mcp-ag.
  --add-account   Добавить ещё один аккаунт Telegram (второй, третий...):
                  спросит название нового аккаунта и войдёт в него.
  --relogin       Войти в Telegram заново (например, если сессия отозвана
                  вручную в Settings -> Devices) -- даже если config.env
                  уже существует. Если аккаунтов несколько, спросит, в какой.
  --account МЕТКА Вместе с --relogin: войти заново именно в этот аккаунт.
  --uninstall     Снять регистрации из клиентов и предложить удалить
                  папку $INSTALL_DIR.
  -h, --help      Показать эту справку.
EOF
}

# Fills ACTION and ACCOUNT_ARG, or explains what is wrong and exits. Written
# with a while/shift loop because --account takes a value.
parse_args() {
    ACTION="install"
    ACCOUNT_ARG=""
    local uninstall=0 relogin=0 add=0 account_given=0
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --uninstall) uninstall=1 ;;
            --relogin) relogin=1 ;;
            --add-account) add=1 ;;
            --account)
                if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
                    error "После --account нужно название аккаунта, например: --relogin --account work"
                    exit 1
                fi
                ACCOUNT_ARG="$2"
                account_given=1
                shift
                ;;
            --account=*)
                ACCOUNT_ARG="${1#--account=}"
                account_given=1
                ;;
            -h | --help)
                print_help
                exit 0
                ;;
            *)
                error "Неизвестный флаг: $1"
                print_help
                exit 1
                ;;
        esac
        shift
    done

    if [[ "$account_given" -eq 1 && ! "$ACCOUNT_ARG" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then
        error "Название аккаунта -- латинские буквы, цифры, \"_\" и \"-\", не длиннее 32 символов. Например, \"рабочий\" -> work."
        exit 1
    fi
    if [[ "$account_given" -eq 1 && "$relogin" -eq 0 ]]; then
        error "--account работает только вместе с --relogin. Чтобы добавить новый аккаунт, используйте --add-account."
        exit 1
    fi
    if [[ $((uninstall + relogin + add)) -gt 1 ]]; then
        error "Флаги --uninstall, --relogin и --add-account нельзя сочетать: выберите один."
        exit 1
    fi
    if [[ "$uninstall" -eq 1 ]]; then
        ACTION="uninstall"
    elif [[ "$add" -eq 1 ]]; then
        ACTION="add-account"
    elif [[ "$relogin" -eq 1 ]]; then
        ACTION="relogin"
    fi
}

main() {
    parse_args "$@"

    detect_os
    require_tty

    if [[ "$ACTION" == "uninstall" ]]; then
        do_uninstall
        exit 0
    fi

    setup_python_and_venv
    install_package
    _confirm_new_venv

    case "$ACTION" in
        add-account) add_account_flow ;;
        relogin) relogin_flow ;;
        *) install_login_flow ;;
    esac

    register_claude_code
    register_codex
    register_claude_desktop
    install_claude_skills
    install_codex_skills
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
