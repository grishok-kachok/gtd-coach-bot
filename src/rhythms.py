"""Ритмы коуча: во сколько он пишет. Дом — мозг, а не `.env` на сервере.

Раньше расписание лежало в переменных окружения и читалось один раз на старте.
Значит поменять его мог только тот, у кого есть ssh, и только с перезапуском —
то есть Василий не мог никак. А ритм — это настройка про человека, а не про
инфраструктуру: «пиши мне три раза в день» должно работать как фраза в телеграме.

Формат — строгий YAML со схемой, и это не вкусовщина (решение из PROJECT.md
от 31.07): файл читает КОД. Опечатка в прозе — бот молча перестал писать
по утрам, и это всплывёт через три дня. Опечатка в YAML со схемой — жалоба
сразу и вслух, а расписание остаётся прежним, пока файл не починят.

Ключи по-русски намеренно: файл правят Василий и коуч, а не программист.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

RHYTHMS_FILE = "ритмы.md"

# Умолчания = то, что стояло в .env до переезда. Нужны, только если файла ещё нет
# или он сломан: без расписания бот не должен онеметь.
DEFAULTS = {
    "утро": "10:00",
    "день": "14:30",
    "вечер": "20:00",
    "ночная_выжимка": "03:00",
    "дожим_минут": 30,
    "тихий_час": 23,
}

TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def _complaints(data: dict) -> list[str]:
    """Претензии к файлу человеческим языком. Пусто — значит расписание годное."""
    problems = []
    for key in DEFAULTS:
        if key not in data:
            problems.append(f"нет строки «{key}»")
    for key in ("утро", "день", "вечер", "ночная_выжимка"):
        value = data.get(key)
        if key in data and not (isinstance(value, str) and TIME.match(value)):
            problems.append(f"«{key}: {value}» — нужно время вида ЧЧ:ММ в кавычках")
    minutes = data.get("дожим_минут")
    if "дожим_минут" in data and not (isinstance(minutes, int) and 5 <= minutes <= 180):
        problems.append(f"«дожим_минут: {minutes}» — нужно целое число от 5 до 180")
    hour = data.get("тихий_час")
    if "тихий_час" in data and not (isinstance(hour, int) and 12 <= hour <= 23):
        problems.append(f"«тихий_час: {hour}» — нужно целое число от 12 до 23")
    unknown = set(data) - set(DEFAULTS)
    if unknown:
        problems.append("лишние строки, которых код не знает: " + ", ".join(sorted(unknown)))
    return problems


def path_in(brain_dir: Path) -> Path:
    return brain_dir / "память" / "состояние" / RHYTHMS_FILE


def read(brain_dir: Path) -> tuple[dict, list[str]]:
    """Расписание и претензии к файлу.

    Претензии есть — отдаём УМОЛЧАНИЯ, а не половину файла: расписание, собранное
    из годных строк и подставленных умолчаний, выглядит рабочим и врёт про то,
    когда придёт пинг.
    """
    path = path_in(brain_dir)
    if not path.exists():
        return dict(DEFAULTS), []

    block = YAML_BLOCK.search(path.read_text(encoding="utf-8"))
    if not block:
        return dict(DEFAULTS), ["в файле нет блока ```yaml — расписание читать неоткуда"]
    try:
        data = yaml.safe_load(block.group(1))
    except yaml.YAMLError as err:
        return dict(DEFAULTS), [f"YAML не разбирается: {err}"]
    if not isinstance(data, dict):
        return dict(DEFAULTS), ["в блоке ```yaml должен быть список строк «ключ: значение»"]

    problems = _complaints(data)
    if problems:
        return dict(DEFAULTS), problems
    return {key: data[key] for key in DEFAULTS}, []


def describe(rhythms: dict, previous: dict | None = None) -> str:
    """Человеческая строка про расписание — её коуч показывает Василию."""
    names = {
        "утро": "утренний чек-ин",
        "день": "дневной",
        "вечер": "вечерний",
        "ночная_выжимка": "ночная выжимка",
        "дожим_минут": "дожим через, мин",
        "тихий_час": "после этого часа не дожимаю",
    }
    if previous is None:
        return ", ".join(f"{names[k]} {rhythms[k]}" for k in DEFAULTS)
    changed = [k for k in DEFAULTS if rhythms[k] != previous[k]]
    return ", ".join(f"{names[k]}: {previous[k]} → {rhythms[k]}" for k in changed)
