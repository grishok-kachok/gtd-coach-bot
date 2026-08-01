"""Счёт контекста в токенах: факт от модели, а не пересчёт байтов.

Главная тонкость — что считать «контекстом». Кэш делит ЦЕНУ, но не ОБЪЁМ:
прочитанное из кэша заезжает в голову модели целиком. Считать контекстом
один лишь `input` значило бы объявить, что после первого хода сессия ничего
не весит, — и режимы стало бы не с чем сравнивать.
"""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from src.context_cost import ContextCost, контекст, разобрать


def сегодня() -> str:
    return datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()


USAGE = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 30000,
    "cache_read_input_tokens": 8000,
    "output_tokens": 300,
}


def test_разбор_даёт_четыре_числа():
    числа = разобрать(USAGE)
    assert числа == {"input": 12, "cache_create": 30000, "cache_read": 8000, "output": 300}


def test_контекст_складывает_всё_приехавшее():
    assert контекст(разобрать(USAGE)) == 38012


def test_чужой_формат_не_роняет():
    """Форма поля — договор чужого SDK, и имена ключей уже менялись."""
    assert разобрать(None) == {}
    assert разобрать("сюрприз") == {}
    assert разобрать({}) == {"input": 0, "cache_create": 0, "cache_read": 0, "output": 0}


def test_второе_имя_ключа_тоже_понимается():
    числа = разобрать({"inputTokens": 5, "cacheReadInputTokens": 7})
    assert числа["input"] == 5 and числа["cache_read"] == 7


def test_запись_и_чтение_суток(tmp_path):
    журнал = ContextCost(tmp_path / "coach.db")
    asyncio.run(журнал.записать("telegram", "рабочий", "claude-fable-5", USAGE))
    строки = журнал.сутки(сегодня())
    assert строки == [("telegram", "рабочий", 1, 38012, 300)]


def test_первый_ход_отличается_от_середины_разговора(tmp_path):
    """Мерить режим по середине разговора бессмысленно: померишь болтовню.

    Первый ход отмечает сам движок, а не догадка по кэшу: первый ход
    с цепочкой вызовов инструментов кэш читает вовсю.
    """
    журнал = ContextCost(tmp_path / "coach.db")
    asyncio.run(журнал.записать("telegram", "рабочий", "m", {
        "input_tokens": 100, "cache_creation_input_tokens": 38000,
        "cache_read_input_tokens": 0, "output_tokens": 10}, 38100, True))
    asyncio.run(журнал.записать("telegram", "рабочий", "m", {
        "input_tokens": 50, "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 38000, "output_tokens": 10}, 38250, False))
    assert журнал.первый_ход("рабочий") == 38100
    assert журнал.первый_ход("полный") is None


def test_пустой_usage_не_пишется(tmp_path):
    журнал = ContextCost(tmp_path / "coach.db")
    assert asyncio.run(журнал.записать("telegram", "рабочий", "m", None)) == {}
    assert журнал.сутки(сегодня()) == []


def test_рюкзак_и_ход_считаются_отдельно(tmp_path):
    """Ход коуча — цепочка: сходил в Todoist, заглянул в календарь, ответил.
    Каждый шаг заезжает в модель заново, и итоговый usage считает их все.
    Живой круг 02.08.2026: рюкзак 41 497, весь ход 126 516. Потолок стоит
    на рюкзаке — он про то, что мы положили, а не про усердие модели.
    """
    журнал = ContextCost(tmp_path / "coach.db")
    asyncio.run(журнал.записать("telegram", "рабочий", "m", {
        "input_tokens": 200, "cache_creation_input_tokens": 80000,
        "cache_read_input_tokens": 46316, "output_tokens": 500}, 41497, True))
    assert журнал.первый_ход("рабочий") == 41497          # рюкзак
    строки = журнал.сутки(сегодня())
    assert строки[0][3] == 126516                          # весь ход


def test_старая_база_дотягивается_до_свежей_схемы(tmp_path):
    """Колонка рюкзака добавилась после того, как таблица уже жила."""
    import sqlite3
    путь = tmp_path / "coach.db"
    with sqlite3.connect(путь) as db:
        db.execute("CREATE TABLE context_cost (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "at TEXT NOT NULL, day TEXT NOT NULL, channel TEXT NOT NULL, "
                   "mode TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', "
                   "input INTEGER, cache_create INTEGER, cache_read INTEGER, output INTEGER)")
        db.execute("INSERT INTO context_cost(at, day, channel, mode, input, cache_create,"
                   " cache_read, output) VALUES('t','2026-08-01','telegram','рабочий',1,2,3,4)")
    журнал = ContextCost(путь)          # миграция на открытии
    asyncio.run(журнал.записать("telegram", "рабочий", "m", USAGE, 40000, True))
    assert журнал.первый_ход("рабочий") == 40000
