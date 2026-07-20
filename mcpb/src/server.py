"""Точка входа бандла .mcpb.

Claude Desktop запускает этот файл через `uv run`, поэтому каталог `src/`
оказывается первым в `sys.path` и пакет `telegram_mcp_ag`, положенный сюда
сборщиком (build.sh), импортируется без установки.

Ничего, кроме запуска, здесь быть не должно: вся логика живёт в пакете.
"""

from telegram_mcp_ag.server import main

if __name__ == "__main__":
    main()
