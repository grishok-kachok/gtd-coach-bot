"""Ночная проверка памяти: разбор ответа модели и запись в копилки.

Модель здесь не зовём — проверяем то, что ломается молча: разбор разделов
и то, что «нет» не превращается в запись. Пустой раздел, записанный как факт,
через месяц даст копилку из тридцати строк «- нет».
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from src.memory_watch import MISSES, PROPOSALS, MemoryWatch

ANSWER = """ФАКТЫ:
- Вылет на Бали 17 сентября, билеты куплены — сказал сам
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
    assert "Со слов Василия" in proposals and "17 сентября" in proposals
    assert "Выводы коуча — сначала спросить" in proposals
    assert "задачи без первого шага" in proposals
    # Вывод не должен затесаться в раздел фактов — иначе бот запишет за Василия то,
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
