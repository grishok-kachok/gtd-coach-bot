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
        if path == "/tasks":
            # Перебор, а не поиск: `search:` у Todoist не находит только что
            # созданную задачу (прогон 02.08.2026), и правило «одна задача
            # на копилку» ломалось молча.
            return {"results": list(self.tasks)}
        if path == "/tasks/filter":
            raise AssertionError(
                "backstage снова ищет поиском — он врёт на свежих задачах")
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


def test_вторая_находка_не_плодит_задачу_а_комментирует(todoist, tmp_path, monkeypatch):
    # Реестр своих комментариев с этапа 10 живёт в архивной базе — в тесте
    # уводим её в tmp, иначе код полезет в /archive контейнера.
    monkeypatch.setenv("ARCHIVE_DB", str(tmp_path / "coach.db"))
    asyncio.run(backstage.raise_task("токен", "предложения", "первая ночь"))
    исход = asyncio.run(backstage.raise_task("токен", "предложения", "вторая ночь"))

    assert исход == "дополнена"
    assert len(todoist.created) == 1, "вторая задача на ту же копилку — засорение"
    # Маркер обязателен: этот комментарий пишет бот, и через три минуты его
    # увидит опрос канала. По автору своё от хозяйского не отличается —
    # бот работает токеном пользователя.
    assert todoist.comments == [{"task_id": "t1", "content": f"{backstage.МАРКЕР} вторая ночь"}]
    from src.comment_state import Журнал
    assert Журнал(tmp_path / "coach.db").свой("c1") is True


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


def test_задача_ищется_перебором_а_не_поиском():
    """Todoist не находит `search:` только что созданную задачу.

    Прогон 02.08.2026: две задачи заведены подряд, поиск не нашёл ни одной,
    перебор нашёл обе. То есть правило «одна задача на копилку, а не на
    находку» молча ломалось ровно в том окне, когда две находки приходят
    подряд, — а именно так они и приходят.
    """
    спрошено = []

    class Клиент:
        async def get(self, путь, params=None):
            спрошено.append((путь, params))
            return {"results": [{"id": "1", "content": "Недельный обзор"}]}

    найдено = asyncio.run(backstage._open_task(Клиент(), "Недельный обзор"))
    assert найдено and найдено["id"] == "1"
    пути = [п for п, _ in спрошено]
    assert "/tasks" in пути
    assert "/tasks/filter" not in пути, "снова ищем поиском — он врёт на свежих задачах"


def test_чужая_задача_с_похожим_названием_не_считается():
    class Клиент:
        async def get(self, путь, params=None):
            return {"results": [{"id": "1", "content": "Недельный обзор с Таней"}]}

    assert asyncio.run(backstage._open_task(Клиент(), "Недельный обзор")) is None


def test_у_каждого_обзора_свой_повод():
    """Три отдельных повода, а не один список: у каждого свой разговор."""
    for ключ in ("недельный обзор", "месячный итог", "годовая стратсессия"):
        находка = backstage.FINDINGS[ключ]
        assert находка.title and находка.why
        assert "режим" in находка.why, f"«{ключ}» не говорит, куда переключаться"


# --- обновления продукта ---


def test_обновление_и_движок_это_два_разных_повода():
    """Действия разные: репозитории тянет ./update.sh, движок поднимает человек
    осознанным коммитом. Одна задача на двоих сказала бы «обновись», не сказав,
    что руками надо сделать разное."""
    обновление, движок = backstage.FINDINGS["обновление"], backstage.FINDINGS["движок"]
    assert обновление.title != движок.title
    assert "update.sh" in обновление.why, "задача обязана говорить, чем обновляться"
    assert "update.sh" not in движок.why, "движок командой не поднимается — он запинен"
    assert "коммит" in движок.why


def подделать_отчёт(monkeypatch, отчёт):
    """Подменить сверку версий: сеть в тестах не трогаем."""
    from src import main as главный

    async def проверить(*_а, **_к):
        return отчёт

    monkeypatch.setattr(главный.versions, "проверить", проверить)
    return главный


class ТолькоТокен:
    """Ночная сверка из всего бота берёт один токен — больше ей ничего не надо."""

    todoist_token = "токен"


def test_ночная_сверка_ставит_по_задаче_на_повод(todoist, monkeypatch):
    from src import versions

    главный = подделать_отчёт(monkeypatch, [
        versions.Состояние(имя="плагин", стоит="aaaaaaa", новее="bbbbbbb", отстал_на=3),
        versions.Состояние(имя="движок", стоит="2.1.216", новее="2.2.0"),
    ])
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    названия = [к["content"] for к in todoist.created]
    assert названия == [
        backstage.FINDINGS["обновление"].title,
        backstage.FINDINGS["движок"].title,
    ]
    assert "плагин" in todoist.created[0]["description"]


def test_три_новых_коммита_не_дают_трёх_задач(todoist, monkeypatch):
    """Правило «одна задача на повод». Копилка, в которую сыплются одинаковые
    карточки, перестаёт читаться — так умерли семь карточек-маяков."""
    from src import versions

    главный = подделать_отчёт(monkeypatch, [
        versions.Состояние(имя="бот", стоит="aaaaaaa", новее="bbbbbbb", отстал_на=3),
    ])
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    assert len(todoist.created) == 1, "вторая ночь завела вторую задачу"
    assert len(todoist.comments) == 1, "вторая ночь должна дописать комментарий"


def test_всё_свежее_и_задач_не_появляется(todoist, monkeypatch):
    from src import versions

    главный = подделать_отчёт(monkeypatch, [
        versions.Состояние(имя="бот", стоит="aaaaaaa"),
        versions.Состояние(имя="движок", стоит="2.1.216"),
    ])
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    assert todoist.created == []


def test_упавшая_сверка_не_роняет_ночной_прогон(todoist, monkeypatch):
    """Ночью после неё идут выжимка недели, профиль и причёска истории.
    Уронить всё это из-за недоступного GitHub — плохой размен."""
    from src import main as главный

    async def падает(*_а, **_к):
        raise RuntimeError("GitHub лёг")

    monkeypatch.setattr(главный.versions, "проверить", падает)
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    assert todoist.created == []
