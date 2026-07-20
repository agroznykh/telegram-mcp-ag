#!/usr/bin/env bash
#
# Собрать бандл .mcpb для Claude Desktop.
#
#   bash mcpb/build.sh
#
# Результат: dist/telegram-mcp-ag-<version>.mcpb
#
# Требуется Node.js (для CLI mcpb, запускается через npx — ставить заранее не нужно).

set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$BUNDLE_DIR")"
OUT_DIR="$REPO_DIR/dist"

die() { printf 'Ошибка: %s\n' "$1" >&2; exit 1; }

command -v npx >/dev/null 2>&1 || die "нужен Node.js — https://nodejs.org"

# Версия пакета и версия манифеста должны совпадать: пользователь видит вторую,
# а получает первую, и расхождение потом не отследить.
pkg_version="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$REPO_DIR/pyproject.toml" | head -1)"
manifest_version="$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$BUNDLE_DIR/manifest.json" | head -1)"
[ -n "$pkg_version" ] || die "не смог прочитать version из pyproject.toml"
if [ "$pkg_version" != "$manifest_version" ]; then
    die "версия в pyproject.toml ($pkg_version) не совпадает с manifest.json ($manifest_version)"
fi

# Апстрим закреплён по SHA в двух местах — рассинхрон означает, что бандл
# соберётся не с тем кодом, который протестирован.
pkg_ref="$(grep -o 'telegram-mcp\.git@[0-9a-f]\{40\}' "$REPO_DIR/pyproject.toml" | head -1)"
bundle_ref="$(grep -o 'telegram-mcp\.git@[0-9a-f]\{40\}' "$BUNDLE_DIR/pyproject.toml" | head -1)"
if [ "$pkg_ref" != "$bundle_ref" ]; then
    die "SHA апстрима разошёлся: $pkg_ref в pyproject.toml против $bundle_ref в mcpb/pyproject.toml"
fi

# Исходники кладём в бандл на сборке, а не держим второй копией в репозитории.
printf 'Копирую telegram_mcp_ag в бандл...\n'
rm -rf "$BUNDLE_DIR/src/telegram_mcp_ag"
cp -R "$REPO_DIR/src/telegram_mcp_ag" "$BUNDLE_DIR/src/telegram_mcp_ag"
find "$BUNDLE_DIR/src/telegram_mcp_ag" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# uv.lock едет в бандл, поэтому обновляем его до упаковки: иначе пользователь
# получит версии зависимостей, отличные от тех, на которых бандл проверяли.
if command -v uv >/dev/null 2>&1; then
    printf 'Обновляю uv.lock...\n'
    uv lock --directory "$BUNDLE_DIR"
else
    printf 'uv не найден — собираю с уже имеющимся uv.lock.\n' >&2
    [ -f "$BUNDLE_DIR/uv.lock" ] || die "нет uv.lock и нет uv, чтобы его создать — https://docs.astral.sh/uv/"
fi

printf 'Проверяю манифест...\n'
npx -y @anthropic-ai/mcpb@latest validate "$BUNDLE_DIR/manifest.json"

mkdir -p "$OUT_DIR"
output="$OUT_DIR/telegram-mcp-ag-$pkg_version.mcpb"
printf 'Собираю %s...\n' "$output"
npx -y @anthropic-ai/mcpb@latest pack "$BUNDLE_DIR" "$output"

printf '\nГотово: %s\n' "$output"
