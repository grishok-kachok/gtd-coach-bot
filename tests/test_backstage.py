"""Фоновая находка обязана доехать до Todoist — и ровно одной задачей.

Проверяется главное свойство, ради которого помощник и заводился: повтор
не плодит вторую задачу, а дописывает комментарий. Иначе три ночи подряд
с непустой копилкой дали бы три одинаковые карточки, и человек перестал бы
на них смотреть — ровно так умерли семь карточек-маяков.
"""

import asyncio

import pytest

from src import backstage


class ПоддельныйTodoist:
    """Минимальный Todoist в памяти: задачи, комментарии, проекты."""

    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.comments = []
        self.created = []

    def __call__(self, token):  # backstage зовёт TodoistClient(token)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, params=None):
        if path == "/projects":
            return {"results": [{"id": "p1", "name": "Рабочее"}]}
        if path == "/tasks/filter":
            искомое = (params or {}).get("query", "").removeprefix("search: ")
            return {"results": [t for t in self.tasks if искомое in t["content"]]}
        raise AssertionError(f"неожиданный GET {path}")

    async def post(self, path, json=None):
        if path == "/comments":
            self.comments.append(json)
            return {"id": "c1"}
        if path == "/tasks":
            self.created.append(json)
            self.tasks.append({"id": f"t{len(self.tasks) + 1}", "content": json["content"]})
            return self.tasks[-1]
        raise AssertionError(f"неожиданный POST {path}")


@pytest.fixture
def todoist(monkeypatch):
    поддельный = ПоддельныйTodoist()
    monkeypatch.setattr(backstage, "TodoistClient", поддельный)
    return поддельный


def test_первая_находка_заводит_задачу_с_датой(todoist):
    исход = asyncio.run(backstage.raise_task("токен", "промахи", "промахов 3"))
    assert исход == "создана"
    карточка = todoist.created[0]
    assert карточка["content"] == backstage.FINDINGS["промахи"].title
    assert карточка["due_string"], "задача без даты сама не вернётся"
    assert карточка["project_id"] == "p1"
    assert "промахов 3" in карточка["description"]


def test_вторая_находка_не_плодит_задачу_а_комментирует(todoist):
    asyncio.run(backstage.raise_task("токен", "предложения", "первая ночь"))
    исход = asyncio.run(backstage.raise_task("токен", "предложения", "вторая ночь"))

    assert исход == "дополнена"
    assert len(todoist.created) == 1, "вторая задача на ту же копилку — засорение"
    assert todoist.comments == [{"task_id": "t1", "content": "вторая ночь"}]


def test_похожее_имя_не_считается_той_же_задачей(todoist):
    # search: находит и по кусочку — опознавание должно быть точным.
    todoist.tasks.append({"id": "чужая", "content": "Разобрать заявку к коучу и ещё что-то"})
    исход = asyncio.run(backstage.raise_task("токен", "заявка", "проба"))
    assert исход == "создана"


def test_недоступный_todoist_не_роняет_ночной_прогон(monkeypatch):
    class Падает(ПоддельныйTodoist):
        async def get(self, path, params=None):
            raise backstage.TodoistError("сеть легла")

    monkeypatch.setattr(backstage, "TodoistClient", Падает())
    assert asyncio.run(backstage.raise_task("токен", "потолок", "сумма поехала")) == ""


def test_неизвестный_источник_это_ошибка_программиста(todoist):
    with pytest.raises(KeyError):
        asyncio.run(backstage.raise_task("токен", "выдумка", "текст"))
