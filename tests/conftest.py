import os
import sys
from pathlib import Path

# Тесты запускаются из папки бота: `.venv/bin/pytest`. Пакет `src` лежит рядом,
# устанавливать его ради этого некуда — в контейнере он тоже просто лежит рядом.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Промпты живут в плагине. В контейнере он смонтирован в /plugin, а при работе
# из исходников
# лежит соседней папкой. Ставится ДО импорта src: путь читается на импорте модуля.
os.environ.setdefault(
    "PROMPTS_DIR", str(Path(__file__).resolve().parents[2] / "plugin" / "prompts")
)
# Там же — части конституции и файл состава режимов. Те же правила: путь
# читается на импорте модуля, поэтому ставится до первого импорта src.
os.environ.setdefault(
    "CONSTITUTION_DIR",
    str(Path(__file__).resolve().parents[2] / "plugin" / "prompts" / "конституция"),
)
os.environ.setdefault(
    "MODES_FILE", str(Path(__file__).resolve().parents[2] / "plugin" / "режимы.md")
)

# Пакет Todoist — отдельный репозиторий (один дом). В контейнере он приезжает
# смонтированным клоном в /opt/pkg, при работе из исходников лежит соседней папкой.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "todoist-mcp" / "src"))

# То же для Календаря: с этапа 14 он тоже отдельный репозиторий, а не файл в боте.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gcal-mcp" / "src"))
