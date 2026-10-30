#!/usr/bin/env bash
# Runs the real install steps of install.sh in a home directory whose path has a
# space and Cyrillic letters, with git unusable. Then starts the installed server.
# Stops short of the Telegram login and client registration, which need a person.
#
# Needs TELEGRAM_MCP_AG_REF set to a pushed commit: the installer downloads that
# commit's archive and pyproject.toml from GitHub.
set -euo pipefail

scripts="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
expected_python="3.14"

export HOME="${RUNNER_TEMP:?}/Тест Пользователь"
mkdir -p "$HOME"

# macOS keeps git in /usr/bin next to everything else, so it can't just be taken
# off PATH. This stand-in fails any step that tries to use it.
no_git="$(mktemp -d)"
printf '#!/bin/sh\necho "git must not be needed" >&2\nexit 1\n' >"$no_git/git"
chmod +x "$no_git/git"
export PATH="$no_git:$PATH"

# Sourcing defines everything and skips main().
# shellcheck disable=SC1091
source "$scripts/../../install.sh"

check_server() {
    "$VENV_DIR/bin/python" "$scripts/probe_server.py" "$VENV_DIR/bin/telegram-mcp-ag"
}

setup_python_and_venv
install_package
_confirm_new_venv

[[ -f "$UPSTREAM_SOURCE/pyproject.toml" ]] || { echo "upstream source directory is missing" >&2; exit 1; }
[[ ! -e "$VENV_BACKUP_DIR" ]] || { echo "venv backup was not removed" >&2; exit 1; }

version="$("$VENV_DIR/bin/python" --version)"
echo "$version"
[[ "$version" == "Python $expected_python."* ]] || { echo "expected Python $expected_python" >&2; exit 1; }

# The same call the client-config helpers make.
"$VENV_DIR/bin/python" -m pip install -q tomlkit
check_server

echo "Second run: the upstream copy must be reused and the server must still start."
setup_python_and_venv
install_package
_confirm_new_venv
check_server
