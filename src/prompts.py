"""Тексты поведения коуча живут в плагине. В питоне остаётся только путь.

До 31.07 восемь текстов были константами прямо в коде: три чек-ина, дожимы,
две выжимки, ночная проверка памяти, подпись коммита. Чтобы поменять слово
в утреннем чек-ине, приходилось лезть в `bot/src/main.py` и пересобирать образ.

Но текст утреннего чек-ина — это **поведение коуча**, а дом поведения — плагин
(решение 31.07). Так уже работает конституция: `PROMPT_FILE` указывает
в `/plugin/prompts/coach.md`, том смонтирован только на чтение, правка текста
не требует пересборки питона.

Формат файла — markdown с необязательной YAML-шапкой:

    ---
    system: >
      Короткая роль для этого прогона.
    ---

    Сам текст промпта. Подстановки — фигурными скобками: {day}.

Файл читается **каждый раз заново**, а не кэшируется на старте: правка текста
должна доезжать перезапуском контейнера, а не пересборкой. Чтение маленького
файла раз в сутки ничего не стоит.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", "/plugin/prompts"))

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Полный список того, что код ожидает найти в плагине. Список нужен, чтобы
# нехватка вскрылась на старте одной понятной строкой, а не ночью в 03:00
# внутри ночного прогона.
REQUIRED = (
    "чекин-утро",
    "чекин-день",
    "чекин-вечер",
    "дожим-1",
    "дожим-2",
    "выжимка-дня",
    "выжимка-укрупнение",
    "проверка-памяти",
    "подпись-коммита",
    "месячный-итог",
    "обещание-дня",
)


@dataclass(frozen=True)
class Prompt:
    """Текст промпта плюс роль, с которой его прогоняют."""

    name: str
    text: str
    system: str = ""

    def format(self, **values: object) -> str:
        return self.text.format(**values)


def path_of(name: str) -> Path:
    return PROMPTS_DIR / f"{name}.md"


def load(name: str) -> Prompt:
    """Прочитать промпт из плагина.

    Файла нет — падаем с внятной строкой, а не с трейсбеком про FileNotFound.
    Молча подставлять умолчание нельзя: коуч заговорит не своим голосом,
    и понять это будет неоткуда.
    """
    path = path_of(name)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as err:
        raise SystemExit(f"Промпт «{name}» не читается: {path} ({err})") from err

    system = ""
    head = FRONTMATTER.match(raw)
    if head:
        try:
            meta = yaml.safe_load(head.group(1)) or {}
        except yaml.YAMLError as err:
            raise SystemExit(f"Шапка промпта «{name}» не разбирается: {err}") from err
        if not isinstance(meta, dict):
            raise SystemExit(f"Шапка промпта «{name}» должна быть списком «ключ: значение»")
        system = str(meta.get("system", "")).strip()
        raw = raw[head.end() :]

    text = raw.strip()
    if not text:
        raise SystemExit(f"Промпт «{name}» пустой: {path}")
    return Prompt(name=name, text=text, system=system)


def missing() -> list[str]:
    """Каких промптов не хватает в плагине. Пусто — комплект на месте."""
    return [name for name in REQUIRED if not path_of(name).exists()]


def followups() -> list[Prompt]:
    """Дожимы по порядку: дожим-1, дожим-2, …

    Сколько попыток дожима — теперь данные, а не код. Захотелось третью —
    кладётся `дожим-3.md` рядом, питон не трогается. Порядок числовой,
    а не алфавитный: иначе десятый встал бы между первым и вторым.
    """
    found = []
    for path in PROMPTS_DIR.glob("дожим-*.md"):
        tail = path.stem.removeprefix("дожим-")
        if tail.isdigit():
            found.append((int(tail), path.stem))
    return [load(name) for _, name in sorted(found)]
