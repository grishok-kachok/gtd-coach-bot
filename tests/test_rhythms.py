"""Ритмы в мозге: расписание читает код, значит ошибка обязана быть громкой.

Главное, что здесь стережём, — правило «половина файла хуже отсутствия файла».
Расписание, собранное из годных строк и подставленных умолчаний, выглядит
рабочим и врёт про то, когда придёт пинг.
"""

from __future__ import annotations

import pytest

from src.rhythms import DEFAULTS, describe, read

GOOD = """---
title: ритмы
type: состояние
---

# Ритмы

```yaml
утро: "09:30"
день: "14:30"
вечер: "20:00"
ночная_выжимка: "03:00"
дожим_минут: 30
тихий_час: 23
```
"""


def write(tmp_path, body: str):
    path = tmp_path / "память" / "состояние" / "ритмы.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return tmp_path


def test_missing_file_is_not_a_breakage(tmp_path):
    rhythms, problems = read(tmp_path)
    assert rhythms == DEFAULTS and problems == []


def test_good_file_is_read(tmp_path):
    rhythms, problems = read(write(tmp_path, GOOD))
    assert problems == []
    assert rhythms["утро"] == "09:30" and rhythms["тихий_час"] == 23


@pytest.mark.parametrize(
    "broken, беда",
    [
        (GOOD.replace('утро: "09:30"', 'утро: "25:00"'), "ЧЧ:ММ"),
        (GOOD.replace('утро: "09:30"', "утро: 930"), "ЧЧ:ММ"),
        (GOOD.replace('утро: "09:30"', ""), "нет строки"),
        (GOOD.replace("дожим_минут: 30", "дожим_минут: 500"), "от 5 до 180"),
        (GOOD.replace("тихий_час: 23", "тихий_час: полночь"), "от 12 до 23"),
        (GOOD.replace("утро:", "утречко:"), "лишние строки"),
        (GOOD.replace("```yaml", "```"), "нет блока"),
        (GOOD.replace("дожим_минут: 30", "дожим_минут: [30"), "не разбирается"),
    ],
)
def test_broken_file_complains_and_keeps_defaults(tmp_path, broken, беда):
    rhythms, problems = read(write(tmp_path, broken))
    assert problems, "сломанный файл принят молча"
    assert any(беда in p for p in problems), problems
    assert rhythms == DEFAULTS, "взято полурасписание — оно выглядит рабочим и врёт"


def test_describe_names_only_what_changed():
    fresh = dict(DEFAULTS, утро="08:00")
    assert describe(fresh, DEFAULTS) == "утренний чек-ин: 10:00 → 08:00"
