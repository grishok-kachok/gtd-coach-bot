"""Ночная проверка памяти: разбор ответа модели и запись в копилки.

Модель здесь не зовём — проверяем то, что ломается молча: разбор разделов
и то, что «нет» не превращается в запись. Пустой раздел, записанный как факт,
через месяц даст копилку из тридцати строк «- нет».
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from src.memory_watch import MISSES, PROPOSALS, MemoryWatch, застой

ANSWER = """ФАКТЫ:
- Вылет на Лиссабон 17 сентября, билеты куплены — сказал сам
- Эфиры по вторникам в 10:30

ВЫВОДЫ:
- Похоже, его тормозят задачи без первого шага

ПРОМАХИ:
- Переспросил дату вылета, хотя она записана в состоянии
"""

EMPTY = """ФАКТЫ:
- нет

ВЫВОДЫ:
- нет

ПРОМАХИ:
- нет
"""


@pytest.fixture
def watch(tmp_path):
    w = MemoryWatch(tmp_path / "brain")
    (w.brain_dir / "память" / "знания").mkdir(parents=True)
    (w.brain_dir / "память" / "состояние").mkdir(parents=True)
    (w.brain_dir / "память" / "знания" / "стиль.md").write_text("что знаем", encoding="utf-8")
    return w


def feed(watch, answer):
    async def fake_ask(prompt, system=""):
        return answer
    watch._ask = fake_ask
    return asyncio.run(watch.run(date(2026, 8, 1), "разговор " * 100))


def test_sections_are_split_by_kind(watch):
    found = feed(watch, ANSWER)
    assert found == {"факты": 2, "выводы": 1, "промахи": 1}

    proposals = (watch.journal / PROPOSALS).read_text(encoding="utf-8")
    assert "type: source" in proposals, "предложение — не знание, оно ничего не утверждает"
    assert "Со слов пользователя" in proposals and "17 сентября" in proposals
    assert "Выводы коуча — сначала спросить" in proposals
    assert "задачи без первого шага" in proposals
    # Вывод не должен затесаться в раздел фактов — иначе бот запишет за пользователя то,
    # чего тот не говорил.
    assert proposals.index("17 сентября") < proposals.index("Выводы коуча")

    misses = (watch.journal / MISSES).read_text(encoding="utf-8")
    assert "Переспросил дату вылета" in misses


def test_nothing_found_writes_nothing(watch):
    assert feed(watch, EMPTY) == {"факты": 0, "выводы": 0, "промахи": 0}
    assert not (watch.journal / PROPOSALS).exists()
    assert not (watch.journal / MISSES).exists()


def test_second_night_appends_and_keeps_the_header(watch):
    feed(watch, ANSWER)
    first = (watch.journal / PROPOSALS).read_text(encoding="utf-8")
    feed(watch, ANSWER)
    second = (watch.journal / PROPOSALS).read_text(encoding="utf-8")
    assert second.count("title: предложения-памяти") == 1, "заголовок заметки продублировался"
    assert second.count("## 2026-08-01") == 2
    assert second.startswith(first[:200])


def test_silent_day_is_not_read_at_all(watch):
    assert feed(watch, ANSWER) is not None  # прогрев
    assert asyncio.run(watch.run(date(2026, 8, 2), "ага")) == {"факты": 0, "выводы": 0, "промахи": 0}


# --- норма копилки: невычеркнутое не должно висеть вечно ---
#
# До 04.08 задача вставала только на свежую находку, поэтому её можно было
# закрыть, не разобрав ни строки. Тесты держат обе половины нормы: что считается
# разобранным и с какого возраста молчание становится долгом.

КОПИЛКА = """---
title: предложения-памяти
type: source
---

# Что стоит записать в память

> Собирает ночной прогон. Разобранное вычёркивай и приписывай, чем кончилось.

- строка выше первого дня — часть объяснения, а не находка

## 2026-07-01

**Со слов пользователя — можно записывать сразу:**
- ~~Вылет 17 сентября~~ — в состояние/горизонты.md, 05.07
- Эфиры по вторникам в 10:30

**Выводы коуча — сначала спросить пользователя:**
- Похоже, его тормозят задачи без первого шага

## 2026-07-20

- свежая находка, разбирается на ближайшем обзоре
"""


def копилка(watch, текст=КОПИЛКА, имя=PROPOSALS):
    watch.journal.mkdir(parents=True, exist_ok=True)
    (watch.journal / имя).write_text(текст, encoding="utf-8")
    return watch.journal / имя


def test_вычеркнутое_и_шапка_в_застой_не_идут(watch):
    висит, старая = застой(копилка(watch), date(2026, 8, 1))
    # Вычеркнутая строка разобрана, строка выше первого дня — объяснение,
    # находка от 20.07 ещё свежая. Остаются ровно две.
    assert (висит, старая) == (2, "2026-07-01")


def test_свежая_находка_долгом_не_считается(watch):
    путь = копилка(watch)
    # 20.07 + 13 дней — ещё норма, +14 — уже долг. Границу держим тестом:
    # сторож, который срабатывает на день раньше, начнёт кричать каждую ночь.
    assert застой(путь, date(2026, 8, 2))[0] == 2
    assert застой(путь, date(2026, 8, 3))[0] == 3


def test_разобранная_копилка_молчит(watch):
    разобрано = КОПИЛКА.replace("- Эфиры по вторникам в 10:30",
                                "- ~~Эфиры по вторникам в 10:30~~ — в знания/глоссарий.md, 04.08")
    разобрано = разобрано.replace("- Похоже, его тормозят задачи без первого шага",
                                  "- ~~Похоже, его тормозят задачи без первого шага~~ — не подтвердил, 04.08")
    assert застой(копилка(watch, разобрано), date(2026, 8, 1)) == (0, "")


def test_копилки_нет_это_не_поломка(watch):
    assert застой(watch.journal / PROPOSALS, date(2026, 8, 1)) == (0, "")


def test_промахи_не_меряем(watch):
    # Улики копятся до тех пор, пока не сложится картина, и вычёркивает их
    # человек сам, починив причину. Копилка, которой положено расти, отклонением не бывает.
    копилка(watch, КОПИЛКА, MISSES)
    assert watch.залежалось(date(2026, 8, 1)) == (0, "")


def test_залежалось_смотрит_в_предложения(watch):
    копилка(watch)
    assert watch.залежалось(date(2026, 8, 1)) == (2, "2026-07-01")
