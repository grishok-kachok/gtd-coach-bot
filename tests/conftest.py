import sys
from pathlib import Path

# Тесты запускаются из папки бота: `.venv/bin/pytest`. Пакет `src` лежит рядом,
# устанавливать его ради этого некуда — в контейнере он тоже просто лежит рядом.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
