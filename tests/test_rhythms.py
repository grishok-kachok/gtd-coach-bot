"""Ритмы в мозге: расписание читает код, значит ошибка обязана быть громкой.

Главное, что здесь стережём, — правило «половина файла хуже отсутствия файла».
Расписание, собранное из годных строк и подставленных умолчаний, выглядит
рабочим и врёт про то, когда придёт пинг.

Второе, что стережём с 19.08.2026, — **совместимость со старым файлом**.
Недостающая строка не поломка: человек ничего не написал, значит берём умолчание
и молчим. Иначе каждый новый ключ ломал бы ритмы у всех, кто про него не знает,
разом откатывая на умолчания и остальные строки тоже.
"""

from __future__ import annotations

import pytest

from src import rhythms
from src.rhythms import DEFAULTS, describe, read, выключен

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


СТАРЫЙ = """---
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
"""Файл ровно такой, каким он был до этапа 25 — шесть строк и ни одной новой."""


def test_old_file_works_silently(tmp_path):
    """Главная защита внешнего пользователя: новых ключей нет, жалоб тоже нет."""
    rhythms, problems = read(write(tmp_path, СТАРЫЙ))
    assert problems == [], "старый файл ритмов вызвал жалобу"
    assert rhythms["утро"] == "09:30"
    # Ключи, которых в файле нет, пришли из умолчаний — расписание целое.
    assert set(rhythms) == set(DEFAULTS)


def test_missing_line_is_a_default_not_a_complaint(tmp_path):
    без_утра = СТАРЫЙ.replace('утро: "09:30"\n', "")
    rhythms, problems = read(write(tmp_path, без_утра))
    assert problems == []
    assert rhythms["утро"] == DEFAULTS["утро"]
    assert rhythms["день"] == "14:30", "остальные строки должны остаться своими"


@pytest.mark.parametrize(
    "broken, беда",
    [
        (GOOD.replace('утро: "09:30"', 'утро: "25:00"'), "ЧЧ:ММ"),
        (GOOD.replace('утро: "09:30"', "утро: 930"), "ЧЧ:ММ"),
        (GOOD.replace("дожим_минут: 30", "дожим_минут: 500"), "от 5 до 180"),
        (GOOD.replace("дожим_минут: 30", "дожим_минут: 30\nдожим_раз: -1"), "от 0 до 5"),
        (GOOD.replace("дожим_минут: 30", "дожим_минут: 30\nдожим_раз: много"), "от 0 до 5"),
        (GOOD.replace("дожим_минут: 30", "дожим_минут: 30\nдожим_раз: 20"), "от 0 до 5"),
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


def test_ноль_дожимов_законен(tmp_path):
    """«Мне дожимы не нужны» — это настройка, а не поломка файла."""
    файл = GOOD.replace("дожим_минут: 30", "дожим_минут: 30\nдожим_раз: 0")
    rhythms, problems = read(write(tmp_path, файл))
    assert problems == []
    assert rhythms["дожим_раз"] == 0


def test_дожимов_по_умолчанию_столько_же_сколько_было(tmp_path):
    """У того, кто ничего не менял, поведение прежнее: две попытки."""
    rhythms, problems = read(write(tmp_path, СТАРЫЙ))
    assert problems == [] and rhythms["дожим_раз"] == 2


def test_день_и_вечер_можно_выключить(tmp_path):
    файл = GOOD.replace('день: "14:30"', "день: нет")
    rhythms, problems = read(write(tmp_path, файл))
    assert problems == []
    assert выключен(rhythms, "день")
    assert not выключен(rhythms, "вечер"), "выключился не только тот, кого просили"


def test_вечер_тоже_выключается(tmp_path):
    файл = GOOD.replace('вечер: "20:00"', "вечер: нет")
    rhythms, problems = read(write(tmp_path, файл))
    assert problems == [] and выключен(rhythms, "вечер")


@pytest.mark.parametrize("key", ["утро", "ночная_выжимка"])
def test_утро_и_выжимку_выключить_нельзя(tmp_path, key):
    """Утро держит обещание дня, выжимка — уборка внутри, а не сообщение."""
    было = {"утро": 'утро: "09:30"', "ночная_выжимка": 'ночная_выжимка: "03:00"'}[key]
    rhythms, problems = read(write(tmp_path, GOOD.replace(было, f"{key}: нет")))
    assert any("выключить можно только" in p for p in problems), problems
    assert rhythms == DEFAULTS


def test_чепуха_вместо_времени_подсказывает_про_нет(tmp_path):
    rhythms, problems = read(write(tmp_path, GOOD.replace('день: "14:30"', "день: никогда")))
    assert any("«нет»" in p for p in problems), problems


# ── день ревизии (заявка #173, 22.08.2026) ───────────────────────────────────

def test_день_ревизии_по_умолчанию_среда(tmp_path):
    ритмы, претензии = read(tmp_path)
    assert претензии == []
    assert rhythms.день_ревизии(ритмы) == 2


def test_день_ревизии_меняется_словом(tmp_path):
    write(tmp_path, GOOD.replace("тихий_час: 23", "тихий_час: 23\nдень_ревизии: пятница"))
    ритмы, претензии = read(tmp_path)
    assert претензии == []
    assert rhythms.день_ревизии(ритмы) == 4


def test_день_ревизии_с_опечаткой_жалуется(tmp_path):
    write(tmp_path, GOOD.replace("тихий_час: 23", "тихий_час: 23\nдень_ревизии: срида"))
    ритмы, претензии = read(tmp_path)
    assert претензии and "день_ревизии" in претензии[0]
    assert rhythms.день_ревизии(ритмы) == 2, "расписание обязано остаться прежним"
