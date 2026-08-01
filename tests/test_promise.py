"""Разбор обещания дня: строка либо честная, либо её нет.

Единственное место этапа, где ночью думает модель, — значит единственное,
где ответ может прийти не в той форме. Порченая строка тихо въезжает
в статистику и живёт там месяцами, поэтому здесь строго.
"""

import asyncio
import sqlite3
from datetime import date

import pytest

from src.promise import PromiseWatch


@pytest.fixture
def сторож(tmp_path, monkeypatch):
    страж = PromiseWatch(tmp_path / "coach.db")
    with sqlite3.connect(страж.db_path) as db:
        db.execute("CREATE TABLE todoist_closed (task_id TEXT, completed_at TEXT,"
                   " content TEXT, project TEXT)")
        db.execute("INSERT INTO todoist_closed VALUES('t1','2026-08-01T09:00:00Z',"
                   "'Правки на странице курса','Рабочее')")
    return страж


def _ответ(сторож, monkeypatch, текст):
    async def ask(self, prompt, system):
        return текст
    monkeypatch.setattr(PromiseWatch, "_ask", ask)
    return asyncio.run(сторож.run(date(2026, 8, 1), "разговор " * 100))


def строки(сторож):
    with sqlite3.connect(сторож.db_path) as db:
        return list(db.execute("SELECT day, обещание, исход, главное FROM day_promise"))


def test_обещание_разбирается_по_форме(сторож, monkeypatch):
    итог = _ответ(сторож, monkeypatch,
                  "ОБЕЩАНИЕ: дожать лендинг\nИСХОД: выполнено\nГЛАВНОЕ: да")
    assert итог == {"обещание": "дожать лендинг", "исход": "выполнено", "главное": "да"}
    assert строки(сторож) == [("2026-08-01", "дожать лендинг", "выполнено", "да")]


def test_дня_без_обещания_в_счёте_нет(сторож, monkeypatch):
    """Куплено первым живым прогоном: модель ответила «ОБЕЩАНИЕ: нет,
    ИСХОД: нет», и день без обещания лёг в базу как несдержанный. Из четырёх
    таких дней получилось бы «сдержано 0 из 5»."""
    _ответ(сторож, monkeypatch, "ОБЕЩАНИЕ: нет\nИСХОД: нет\nГЛАВНОЕ: нет данных")
    assert строки(сторож) == [("2026-08-01", "", "неизвестно", "нет данных")]


def test_ответ_не_по_форме_не_записывается(сторож, monkeypatch):
    """Записать «выполнено» наугад хуже, чем не записать вовсе."""
    assert _ответ(сторож, monkeypatch, "Ну, он вроде бы что-то там сделал.") == {}
    assert строки(сторож) == []


def test_короткий_день_не_разбирается(сторож, monkeypatch):
    async def ask(self, prompt, system):
        raise AssertionError("модель звать незачем: разговора не было")
    monkeypatch.setattr(PromiseWatch, "_ask", ask)
    assert asyncio.run(сторож.run(date(2026, 8, 1), "привет")) == {}


def test_закрытые_за_день_попадают_в_вопрос(сторож, monkeypatch):
    увиденное = {}

    async def ask(self, prompt, system):
        увиденное["prompt"] = prompt
        return "ОБЕЩАНИЕ: правки\nИСХОД: выполнено\nГЛАВНОЕ: да"

    monkeypatch.setattr(PromiseWatch, "_ask", ask)
    asyncio.run(сторож.run(date(2026, 8, 1), "разговор " * 100))
    assert "Правки на странице курса" in увиденное["prompt"]


def test_повторный_разбор_переписывает_а_не_двоит(сторож, monkeypatch):
    _ответ(сторож, monkeypatch, "ОБЕЩАНИЕ: раз\nИСХОД: нет\nГЛАВНОЕ: другое")
    _ответ(сторож, monkeypatch, "ОБЕЩАНИЕ: раз\nИСХОД: выполнено\nГЛАВНОЕ: да")
    assert строки(сторож) == [("2026-08-01", "раз", "выполнено", "да")]
