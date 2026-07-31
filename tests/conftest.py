import os
import sys
from pathlib import Path

# Тесты запускаются из папки бота: `.venv/bin/pytest`. Пакет `src` лежит рядом,
# устанавливать его ради этого некуда — в контейнере он тоже просто лежит рядом.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Промпты живут в плагине. В контейнере он смонтирован в /plugin, а в лаборатории
# лежит соседней папкой. Ставится ДО импорта src: путь читается на импорте модуля.
os.environ.setdefault(
    "PROMPTS_DIR", str(Path(__file__).resolve().parents[2] / "plugin" / "prompts")
)

# Пакет Todoist — отдельный репозиторий (один дом). В контейнере он приезжает
# смонтированным клоном в /opt/pkg, в лаборатории лежит соседней папкой.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "todoist-mcp" / "src"))
