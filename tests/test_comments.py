"""Канал комментариев: два сита перед моделью, две защиты от петли, откат.

Главное, что здесь проверяется, — не «работает ли», а **не просыпается ли
модель зря**. Пустой опрос, свой комментарий, чужой комментарий и хозяйский
без обращения обязаны стоить ноль вызовов.
"""

import asyncio
import sqlite3
from datetime import datetime, timedelta

import pytest

from src import comment_state
from src.comment_state import МАРКЕР, Журнал
from src import comments as канал


# --- разбор обращения ---

@pytest.mark.parametrize("текст, ожидание", [
    ("Клод, разбей на подзадачи", "разбей на подзадачи"),
    ("клод разбей на подзадачи", "разбей на подзадачи"),
    ("КЛОД: поставь на завтра", "поставь на завтра"),
    ("@клод — убери срок", "убери срок"),
    ("Клауд, что тут делать", "что тут делать"),
])
def test_обращение_узнаётся(текст, ожидание):
    assert канал.обращение(текст) == ожидание


@pytest.mark.parametrize("текст", [
    "надо не забыть про доверенность",
    "спрошу у Клода потом",           # имя не в начале — это рассказ, а не просьба
    "Клод",                            # позвали, но ничего не попросили
    "",
])
def test_не_обращение(текст):
    assert канал.обращение(текст) is None


# --- поддельный Todoist ---

class ПоддельныйTodoist:
    """Todoist в памяти: комментарии, задачи и один sync_token."""

    ХОЗЯИН = "42"

    def __init__(self):
        self.notes = []            # что отдаст следующий /sync
        self.tasks = {}
        self.created_comments = []
        self.deleted = []
        self.posted = []
        self.token = "T1"

    def __call__(self, token):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, **kwargs):
        if path == "/user":
            return {"id": self.ХОЗЯИН}
        if path.startswith("/tasks/"):
            return dict(self.tasks[path.split("/")[-1]])
        raise AssertionError(f"неожиданный GET {path}")

    async def get_paginated(self, path, params=None, **kwargs):
        if path == "/tasks" and params and "parent_id" in params:
            return [dict(t) for t in self.tasks.values()
                    if str(t.get("parent_id") or "") == str(params["parent_id"])]
        if path == "/comments":
            return []
        return []

    async def post(self, path, **kwargs):
        if path == "/sync":
            заметки, self.notes = self.notes, []
            return {"sync_token": self.token, "notes": заметки, "full_sync": False}
        if path == "/comments":
            тело = kwargs["json"]
            comment_id = f"c{len(self.created_comments) + 1}"
            self.created_comments.append({**тело, "id": comment_id})
            return {"id": comment_id}
        if path.startswith("/tasks/") and path.endswith("/move"):
            self.posted.append((path, kwargs.get("json")))
            self.tasks[path.split("/")[2]].update(kwargs["json"])
            return {}
        if path.startswith("/tasks/"):
            self.posted.append((path, kwargs.get("json")))
            задача = self.tasks[path.split("/")[-1]]
            for имя, значение in kwargs["json"].items():
                if имя == "due_string":
                    задача["due"] = None if значение == "no date" else {"string": значение}
                else:
                    задача[имя] = значение
            return dict(задача)
        raise AssertionError(f"неожиданный POST {path}")

    async def delete(self, path, **kwargs):
        self.deleted.append(path)
        return None


def заметка(id_, текст, автор=ПоддельныйTodoist.ХОЗЯИН, задача="t1", когда="2026-08-01T10:00:00Z"):
    return {"id": id_, "content": текст, "posted_uid": автор,
            "item_id": задача, "posted_at": когда, "is_deleted": False}


class ПоддельныйАрхив:
    def __init__(self):
        self.записи = []

    async def add_message(self, role, channel, text, session_id):
        self.записи.append((role, channel, text))


@pytest.fixture
def канал_с_подделкой(tmp_path, monkeypatch):
    todoist = ПоддельныйTodoist()
    monkeypatch.setattr(канал, "TodoistClient", todoist)
    архив = ПоддельныйАрхив()
    сказанное = []

    async def сказать(текст):
        сказанное.append(текст)

    к = канал.Канал(token="X", db_path=tmp_path / "coach.db", archive=архив, сказать=сказать)
    к.сказанное = сказанное
    return к, todoist, архив


def прогнать(корутина):
    return asyncio.run(корутина)


# --- первый запуск и пустой опрос ---

def test_первый_запуск_историю_не_разбирает(канал_с_подделкой):
    к, todoist, архив = канал_с_подделкой
    todoist.notes = [заметка("n1", "Клод, разбей на подзадачи")]
    итог = прогнать(к.шаг())
    assert итог == {"новых": 0, "обращений": 0, "сделано": 0}
    assert архив.записи == []           # старое не пересказываем
    assert к.журнал.токен() == "T1"     # но закладку поставили


def test_пустой_опрос_ничего_не_делает(канал_с_подделкой):
    к, todoist, архив = канал_с_подделкой
    прогнать(к.шаг())                    # первый — только закладка
    итог = прогнать(к.шаг())
    assert итог == {"новых": 0, "обращений": 0, "сделано": 0}
    assert архив.записи == []


# --- сита ---

def test_свой_комментарий_пропускается_по_реестру(канал_с_подделкой):
    к, todoist, архив = канал_с_подделкой
    прогнать(к.шаг())
    к.журнал.запомнить_свой("n7")
    todoist.notes = [заметка("n7", "Клод, разбей на подзадачи")]
    итог = прогнать(к.шаг())
    assert итог["обращений"] == 0
    assert архив.записи == []


def test_свой_комментарий_пропускается_по_маркеру(канал_с_подделкой):
    """Вторая защита обязана работать в одиночку: реестр пуст, а маркер есть."""
    к, todoist, архив = канал_с_подделкой
    прогнать(к.шаг())
    todoist.notes = [заметка("n8", f"{МАРКЕР} Клод, разбей на подзадачи")]
    итог = прогнать(к.шаг())
    assert итог["обращений"] == 0
    assert архив.записи == []


def test_чужой_комментарий_в_архив_но_не_в_работу(канал_с_подделкой):
    к, todoist, архив = канал_с_подделкой
    прогнать(к.шаг())
    todoist.notes = [заметка("n9", "Клод, поставь на пятницу", автор="777")]
    итог = прогнать(к.шаг())
    assert итог["обращений"] == 0
    assert len(архив.записи) == 1
    роль, канал_записи, текст = архив.записи[0]
    assert канал_записи == "comment" and "не Василий" in текст


def test_хозяйский_без_имени_в_архив_но_не_в_работу(канал_с_подделкой):
    к, todoist, архив = канал_с_подделкой
    прогнать(к.шаг())
    todoist.notes = [заметка("n10", "надо не забыть про доверенность")]
    итог = прогнать(к.шаг())
    assert итог["обращений"] == 0
    assert len(архив.записи) == 1
    assert архив.записи[0][0] == "vasiliy"


# --- разница снимков ---

def test_разница_ловит_созданное_и_изменённое():
    до = {"t1": {"content": "Запуск", "priority": 1}}
    после = {
        "t1": {"content": "Запуск", "priority": 4},
        "t2": {"content": "Собрать темы", "priority": 1},
    }
    итог = канал.разница(до, после)
    assert ("создана", "t2", "", None, после["t2"]) in итог
    assert ("изменено", "t1", "priority", 1, 4) in итог


def test_разница_молчит_когда_ничего_не_менялось():
    слепок = {"t1": {"content": "Запуск", "priority": 1}}
    assert канал.разница(слепок, {"t1": dict(слепок["t1"])}) == []


# --- полный круг: обращение → работа → ответ ---

def test_круг_обращения(канал_с_подделкой, monkeypatch):
    """Обращение разобрано: ответ ушёл комментарием с маркером, в журнале —
    изменение, в архиве — обе реплики, свой ответ занесён в реестр."""
    к, todoist, архив = канал_с_подделкой
    todoist.tasks = {
        "t1": {"id": "t1", "content": "Запуск", "due": None, "priority": 1,
               "labels": [], "project_id": "p1"},
    }
    прогнать(к.шаг())  # закладка

    async def работник(карточка, просьба, task_id):
        # Работник «поменял» приоритет — как это сделала бы модель кнопкой.
        todoist.tasks["t1"]["priority"] = 4
        return "Поднял приоритет до p1."

    monkeypatch.setattr(к, "_работник", работник)
    monkeypatch.setattr(канал.Канал, "_карточка", staticmethod(
        lambda client, task_id: asyncio.sleep(0, result="[t1] Запуск")))

    todoist.notes = [заметка("n1", "Клод, подними приоритет")]
    итог = прогнать(к.шаг())

    assert итог == {"новых": 1, "обращений": 1, "сделано": 1}
    ответ = todoist.created_comments[-1]
    assert ответ["task_id"] == "t1" and ответ["content"].startswith(МАРКЕР)
    assert к.журнал.свой(ответ["id"]) is True
    действия = к.журнал.действия("n1")
    assert any(д.kind == "изменено" and д.field == "priority" for д in действия)
    роли = [(р, т[:20]) for р, к_, т in архив.записи]
    assert роли[0][0] == "vasiliy" and роли[-1][0] == "coach"


# --- откат ---

def test_откат_возвращает_как_было(канал_с_подделкой):
    к, todoist, архив = канал_с_подделкой
    todoist.tasks = {
        "t1": {"id": "t1", "content": "Запуск", "due": {"string": "5 августа"},
               "priority": 1, "labels": [], "project_id": "p1"},
        "t2": {"id": "t2", "content": "Собрать темы", "parent_id": "t1",
               "due": None, "priority": 1, "labels": [], "project_id": "p1"},
    }
    к.журнал.отметить_обращение("n1", "t1", "разбей и поставь на завтра")
    к.журнал.записать("n1", "t1", "изменено", "due_string", "5 августа", "завтра")
    к.журнал.записать("n1", "t2", "создана", "", None, {"content": "Собрать темы"})
    к.журнал.записать("n1", "t1", "комментарий", "", None, "c9")

    отчёт = прогнать(к.откатить())

    assert "/tasks/t2" in todoist.deleted          # созданную подзадачу убрали
    assert "/comments/c9" in todoist.deleted       # свой комментарий убрали
    assert ("/tasks/t1", {"due_string": "5 августа", "due_lang": "ru"}) in todoist.posted
    assert "вернул due_string" in отчёт
    # Дважды не откатываем: записи помечены. И ответ честный — «уже откачено»,
    # а не «я ничего не менял»: это разные вещи, а человек по ответу решает,
    # идти ли чинить руками.
    второй = прогнать(к.откатить())
    assert "уже откачено" in второй


def test_слепой_снимок_не_выдаёт_себя_за_отсутствие_изменений(канал_с_подделкой, monkeypatch):
    """Todoist не ответил после работы — журнал пуст, и об этом обязаны сказать
    вслух: и в комментарии, и в телеграм. Молча это читается как «ничего
    не менялось» (дефект найден живым прогоном 01.08)."""
    к, todoist, архив = канал_с_подделкой
    todoist.tasks = {"t1": {"id": "t1", "content": "Запуск", "due": None,
                            "priority": 1, "labels": [], "project_id": "p1"}}
    прогнать(к.шаг())

    вызовов = {"n": 0}

    async def снимок_с_обрывом(client, task_id):
        вызовов["n"] += 1
        if вызовов["n"] == 1:                      # «до» снимаем нормально
            return {"t1": {"content": "Запуск"}}
        raise канал.TodoistError("Сеть недоступна на GET /tasks: ", retryable=True)

    monkeypatch.setattr(канал, "снимок", снимок_с_обрывом)
    monkeypatch.setattr(канал.Канал, "_карточка", staticmethod(
        lambda client, task_id: asyncio.sleep(0, result="[t1] Запуск")))

    async def работник(карточка, просьба, task_id):
        return "Разбил на шесть шагов."

    monkeypatch.setattr(к, "_работник", работник)
    # Повторы настоящие, но короткие: проверяем поведение, а не терпение.
    monkeypatch.setattr(канал, "retry_network",
                        lambda action, what="": action())

    todoist.notes = [заметка("n1", "Клод, разбей")]
    прогнать(к.шаг())

    ответ = todoist.created_comments[-1]["content"]
    assert "⚠️" in ответ and "откатить" in ответ.lower()
    assert к.сказанное and "откат невозможен" in к.сказанное[0]


def test_откат_без_действий_говорит_об_этом(канал_с_подделкой):
    к, _, _ = канал_с_подделкой
    assert "Откатывать нечего" in прогнать(к.откатить())


def test_обращение_без_изменений_не_мешает_откату(tmp_path):
    """«Откати последнее» должно вернуть последнюю ПРАВКУ, а не упереться
    в уточняющий вопрос, который ничего не менял."""
    журнал = Журнал(tmp_path / "coach.db")
    журнал.отметить_обращение("n1", "t1", "разбей")
    журнал.записать("n1", "t1", "изменено", "priority", 1, 4)
    журнал.отметить_обращение("n2", "t1", "а что тут вообще")
    assert журнал.последнее_обращение() == "n1"


# --- ограничитель ---

def test_потолок_считает_обращения_а_не_изменения(tmp_path):
    журнал = Журнал(tmp_path / "coach.db")
    сейчас = datetime.now(comment_state.MOSCOW)
    for i in range(3):
        журнал.отметить_обращение(f"n{i}", "t1", "вопрос")
    журнал.записать("n0", "t1", "изменено", "priority", 1, 4)
    assert журнал.обращений_за_час(сейчас) == 3


def test_потолок_не_считает_вчерашнее(tmp_path):
    журнал = Журнал(tmp_path / "coach.db")
    журнал.отметить_обращение("n1", "t1", "вопрос")
    завтра = datetime.now(comment_state.MOSCOW) + timedelta(hours=2)
    assert журнал.обращений_за_час(завтра) == 0


def test_упёршись_в_потолок_канал_жалуется(канал_с_подделкой, monkeypatch):
    к, todoist, архив = канал_с_подделкой
    monkeypatch.setattr(канал, "ПОТОЛОК_В_ЧАС", 1)
    к.журнал.отметить_обращение("n0", "t1", "первое")
    assert прогнать(к._в_пределах_потолка()) is False
    assert к.сказанное and "потолок" in к.сказанное[0]


# --- реестр своих ---

def test_реестр_переживает_перезапуск(tmp_path):
    Журнал(tmp_path / "coach.db").запомнить_свой("c1")
    assert Журнал(tmp_path / "coach.db").свой("c1") is True


def test_короткий_вход_для_backstage(tmp_path):
    comment_state.запомнить_свой("c5", tmp_path / "coach.db")
    assert Журнал(tmp_path / "coach.db").свой("c5") is True
