"""Снимок Todoist — несгораемая история дел.

Главное свойство, ради которого он заводился: **удалённая карточка остаётся
в снимке вместе с описанием**. В Todoist она исчезает без следа, а описание
после этапа 16 несёт «зачем эта затея» — то есть смысл, который дороже самой
карточки.

Второе свойство: закрытые за сутки копятся отдельно. Без них план/факт
не посчитать.
"""

import asyncio
import json
import sqlite3
from datetime import date

import pytest

from src import todoist_snapshot as снимок


class ПоддельныйTodoist:
    """Todoist в памяти: набор задач меняется между ночами."""

    def __init__(self):
        self.tasks = []
        self.closed = []
        self.comments = []

    def __call__(self, token):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_paginated(self, path, params=None, key="results", cap=300):
        if path == "/projects":
            return [{"id": "p1", "name": "Рабочее"}]
        if path == "/sections":
            return [{"id": "s1", "name": "Идеи"}]
        if path == "/tasks":
            return list(self.tasks)
        if path == "/comments":
            return list(self.comments)
        if path == "/tasks/completed/by_completion_date":
            return list(self.closed)
        raise AssertionError(f"неожиданный путь {path}")


def задача(task_id, content, **поля):
    основа = {
        "id": task_id, "content": content, "description": "", "project_id": "p1",
        "section_id": "s1", "labels": [], "priority": 1, "due": None, "added_at": "2026-07-01",
    }
    основа.update(поля)
    return основа


@pytest.fixture
def лаборатория(tmp_path, monkeypatch):
    поддельный = ПоддельныйTodoist()
    monkeypatch.setattr(снимок, "TodoistClient", поддельный)
    хранилище = снимок.TodoistSnapshot(tmp_path / "coach.db", "токен")
    return хранилище, поддельный


def строки(хранилище, таблица, day=None):
    with sqlite3.connect(хранилище.db_path) as db:
        db.row_factory = sqlite3.Row
        запрос = f"SELECT * FROM {таблица}"
        if day:
            запрос += f" WHERE day = '{day}'"
        return [dict(r) for r in db.execute(запрос)]


def test_первая_ночь_кладёт_задачи_с_описанием_и_комментариями(лаборатория):
    хранилище, todoist = лаборатория
    todoist.tasks = [задача("t1", "Смена регистрации", description="служит цели: Бали")]
    todoist.comments = [{"task_id": "t1", "content": "звонил в МФЦ"}]

    итог = asyncio.run(хранилище.run(date(2026, 8, 1)))

    assert итог["задач"] == 1
    строка = строки(хранилище, "todoist_tasks")[0]
    assert строка["description"] == "служит цели: Бали"
    assert json.loads(строка["comments"]) == ["звонил в МФЦ"]
    assert строка["project"] == "Рабочее" and строка["section"] == "Идеи"


def test_первая_ночь_диффа_не_даёт(лаборатория):
    хранилище, todoist = лаборатория
    todoist.tasks = [задача("t1", "Что-то")]
    assert asyncio.run(хранилище.run(date(2026, 8, 1)))["изменений"] == 0


def test_удалённая_карточка_переживает_удаление(лаборатория):
    """То, ради чего всё: из Todoist исчезло — в снимке осталось."""
    хранилище, todoist = лаборатория
    todoist.tasks = [задача("t1", "Автопродукт", description="зачем: доход осенью")]
    asyncio.run(хранилище.run(date(2026, 8, 1)))

    todoist.tasks = []  # владелец снёс карточку
    asyncio.run(хранилище.run(date(2026, 8, 2)))

    вчера = строки(хранилище, "todoist_tasks", "2026-08-01")
    assert вчера[0]["description"] == "зачем: доход осенью"

    дифф = строки(хранилище, "todoist_diff", "2026-08-02")
    assert [(d["kind"], d["content"]) for d in дифф] == [("исчезла", "Автопродукт")]


def test_закрытая_отличается_от_удалённой(лаборатория):
    хранилище, todoist = лаборатория
    todoist.tasks = [задача("t1", "Оплатить эквайринг")]
    asyncio.run(хранилище.run(date(2026, 8, 1)))

    todoist.tasks = []
    todoist.closed = [{"id": "t1", "completed_at": "2026-08-02T09:00:00",
                       "content": "Оплатить эквайринг", "project_id": "p1"}]
    asyncio.run(хранилище.run(date(2026, 8, 2)))

    дифф = строки(хранилище, "todoist_diff", "2026-08-02")
    assert [d["kind"] for d in дифф] == ["закрыта"]
    assert строки(хранилище, "todoist_closed")[0]["content"] == "Оплатить эквайринг"


def test_перенос_срока_виден_отдельной_строкой(лаборатория):
    хранилище, todoist = лаборатория
    todoist.tasks = [задача("t1", "Карусель", due={"date": "2026-08-03"})]
    asyncio.run(хранилище.run(date(2026, 8, 1)))

    todoist.tasks = [задача("t1", "Карусель", due={"date": "2026-08-07"})]
    asyncio.run(хранилище.run(date(2026, 8, 2)))

    дифф = {d["kind"]: d["detail"] for d in строки(хранилище, "todoist_diff", "2026-08-02")}
    assert дифф["перенесена"] == "2026-08-03 → 2026-08-07"


def test_повторный_прогон_за_тот_же_день_не_двоит(лаборатория):
    хранилище, todoist = лаборатория
    todoist.tasks = [задача("t1", "Одна")]
    asyncio.run(хранилище.run(date(2026, 8, 1)))
    asyncio.run(хранилище.run(date(2026, 8, 1)))
    assert len(строки(хранилище, "todoist_tasks", "2026-08-01")) == 1


def test_недоступный_todoist_не_роняет_ночной_прогон(лаборатория, monkeypatch):
    хранилище, _ = лаборатория

    class Падает(ПоддельныйTodoist):
        async def get_paginated(self, *a, **kw):
            raise снимок.TodoistError("сеть легла")

    monkeypatch.setattr(снимок, "TodoistClient", Падает())
    assert asyncio.run(хранилище.run(date(2026, 8, 3)))["задач"] == 0
