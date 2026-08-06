"""Фоновая находка обязана доехать до Todoist — и ровно одной задачей.

Проверяется главное свойство, ради которого помощник и заводился: повтор
не плодит вторую задачу, а дописывает комментарий. Иначе три ночи подряд
с непустой копилкой дали бы три одинаковые карточки, и человек перестал бы
на них смотреть — ровно так умерли семь карточек-маяков.
"""

import asyncio
import sqlite3

import pytest

from src import backstage, detectors


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

    @property
    def находки(self):
        """Созданные карточки без служебной крыши: тесты писались про находки."""
        return [з for з in self.created if з["content"] != backstage.КРЫША]

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
    карточка = todoist.находки[0]
    assert карточка["content"] == backstage.FINDINGS["промахи"].title
    assert карточка["due_string"], "задача без даты сама не вернётся"
    assert "промахов 3" in карточка["description"]
    # Промахи чинятся правкой кода — значит подзадача под крышей, а место
    # ей задаёт родитель. В проект кладётся сама крыша.
    assert карточка["parent_id"]
    крыша = [з for з in todoist.created if з["content"] == backstage.КРЫША][0]
    assert крыша["project_id"] == "p1"


def test_вторая_находка_не_плодит_задачу_а_комментирует(todoist, tmp_path, monkeypatch):
    # Реестр своих комментариев с этапа 10 живёт в архивной базе — в тесте
    # уводим её в tmp, иначе код полезет в /archive контейнера.
    monkeypatch.setenv("ARCHIVE_DB", str(tmp_path / "coach.db"))
    asyncio.run(backstage.raise_task("токен", "предложения", "первая ночь"))
    исход = asyncio.run(backstage.raise_task("токен", "предложения", "вторая ночь"))

    assert исход == "дополнена"
    assert len(todoist.находки) == 1, "вторая задача на ту же копилку — засорение"
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


# --- готовый текст для того, кто чинит код ---

# Поводы, которые человек закрывает сам, без сессии с кодом: свои просроченные
# дела и три разговора с коучем. Список нужен, чтобы сторож ниже отличал
# «текста нет намеренно» от «текст забыли написать».
БЕЗ_АГЕНТА = {"завал", "недельный обзор", "месячный итог", "годовая стратсессия"}


def test_у_технической_находки_есть_готовый_текст():
    """Иначе человек пересказывает техническую беду своими словами — а он в ней
    и не разбирается. Пересказ и есть то место, где теряются подробности."""
    без_текста = sorted(к for к, н in backstage.FINDINGS.items()
                        if not н.агенту and к not in БЕЗ_АГЕНТА)
    assert без_текста == [], f"находки без задания агенту: {без_текста}"


def test_человеческий_повод_не_тащит_текст_для_кода():
    """Разговор с коучем чинить кодом не надо — лишний блок только путает."""
    for ключ in sorted(БЕЗ_АГЕНТА):
        assert not backstage.FINDINGS[ключ].агенту


def test_описание_несёт_текст_целиком_и_находку():
    текст = backstage.описание(backstage.FINDINGS["потолок"], "в паспорте 1168 Б, на деле 2069 Б")
    assert backstage.ШАПКА_АГЕНТУ in текст
    assert "startup_budget.py" in текст          # где чинить
    assert "в паспорте 1168 Б, на деле 2069 Б" in текст   # что нашли этой ночью
    assert "комментарии" in текст                # где искать находки следующих ночей


def test_описание_человеческого_повода_без_блока():
    текст = backstage.описание(backstage.FINDINGS["завал"], "висит третьи сутки")
    assert backstage.ШАПКА_АГЕНТУ not in текст
    assert "висит третьи сутки" in текст


def test_заведённая_задача_несёт_текст_в_описании(todoist):
    """Проверка сквозь: текст обязан доехать до карточки, а не остаться в коде."""
    asyncio.run(backstage.raise_task("токен", "промахи", "промахов 3"))
    карточка = todoist.находки[0]
    assert backstage.ШАПКА_АГЕНТУ in карточка["description"]
    assert "memory_watch.py" in карточка["description"]
    assert "промахов 3" in карточка["description"]


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
    названия = [к["content"] for к in todoist.находки]
    assert названия == [
        backstage.FINDINGS["обновление"].title,
        backstage.FINDINGS["движок"].title,
    ]
    assert "плагин" in todoist.находки[0]["description"]


def test_три_новых_коммита_не_дают_трёх_задач(todoist, monkeypatch):
    """Правило «одна задача на повод». Копилка, в которую сыплются одинаковые
    карточки, перестаёт читаться — так умерли семь карточек-маяков."""
    from src import versions

    главный = подделать_отчёт(monkeypatch, [
        versions.Состояние(имя="бот", стоит="aaaaaaa", новее="bbbbbbb", отстал_на=3),
    ])
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    assert len(todoist.находки) == 1, "вторая ночь завела вторую задачу"
    assert len(todoist.comments) == 1, "вторая ночь должна дописать комментарий"


def test_всё_свежее_и_задач_не_появляется(todoist, monkeypatch):
    from src import versions

    главный = подделать_отчёт(monkeypatch, [
        versions.Состояние(имя="бот", стоит="aaaaaaa"),
        versions.Состояние(имя="движок", стоит="2.1.216"),
    ])
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    assert todoist.находки == []


def test_упавшая_сверка_не_роняет_ночной_прогон(todoist, monkeypatch):
    """Ночью после неё идут выжимка недели, профиль и причёска истории.
    Уронить всё это из-за недоступного GitHub — плохой размен."""
    from src import main as главный

    async def падает(*_а, **_к):
        raise RuntimeError("GitHub лёг")

    monkeypatch.setattr(главный.versions, "проверить", падает)
    asyncio.run(главный.CoachBot._сверить_версии(ТолькоТокен()))
    assert todoist.находки == []


# ── размер задачи и копилка поломок (этап 20) ────────────────────────────────


def test_у_задачи_есть_метка_размера(todoist):
    """Бот ставил свои задачи без размера — и сам же считал их отклонением.

    Детектор `без_размера` смотрит на активные задачи без `⏱️`, а `перегруз`
    считает день по этим же меткам: задача без метки весит ноль часов. То есть
    бот рисовал себе «без размера 4 из 7» и одновременно занижал загрузку дня.
    """
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222"))
    метки = todoist.находки[0]["labels"]
    assert any(м.startswith("⏱️") for м in метки), "задача снова без размера"


def test_у_задачи_бота_нет_метки_состояния(todoist):
    """Срок сильнее метки (этап 21): у задачи бота есть `due_string`, значит
    метке состояния там не место. Раньше бот вешал `актив` и сам себе рисовал
    отклонение «срок и метка» на каждой ночной находке."""
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222"))
    метки = todoist.находки[0]["labels"]
    assert not (set(метки) & set(detectors.СОСТОЯНИЯ)), f"метка состояния: {метки}"
    assert todoist.находки[0]["due_string"], "срок обязан быть — на нём всё держится"


def test_размер_по_вкладу_человека_а_не_по_объёму(todoist):
    """Заявка владельца 04.08: человеко-часы против машино-часов."""
    assert backstage.FINDINGS["движок"].размер == backstage.S, "движок поднимает агент"
    assert backstage.FINDINGS["оглавление"].размер == backstage.S
    assert backstage.FINDINGS["недельный обзор"].размер == backstage.M, "человек сидит сам"
    assert backstage.FINDINGS["завал"].размер == backstage.M
    assert backstage.FINDINGS["месячный итог"].размер == backstage.L


def test_поломка_ложится_на_полку_и_считает_повторы(todoist, tmp_path):
    from src.inbox import ПОЛОМКА, Inbox

    полка = Inbox(tmp_path / "coach.db")
    asyncio.run(backstage.raise_task("t", "ритмы", "файл не читается", inbox=полка))
    asyncio.run(backstage.raise_task("t", "ритмы", "файл не читается", inbox=полка))

    записи = полка.открытые(ПОЛОМКА)
    assert len(записи) == 1, "вторая поломка завела вторую запись вместо счётчика"
    assert записи[0]["случаев"] == 2
    # Второй раз задача уже стоит — значит про повтор говорит комментарий.
    assert "2-й раз" in todoist.comments[0]["content"]


def test_первый_случай_про_повторы_молчит(todoist, tmp_path):
    """«Случилось 1-й раз» — шум: сторож, который говорит лишнее, не читается."""
    from src.inbox import Inbox

    asyncio.run(backstage.raise_task("t", "ритмы", "файл не читается",
                                     inbox=Inbox(tmp_path / "coach.db")))
    assert "раз" not in todoist.находки[0]["description"].split("Норма")[0][-40:]


def test_обновление_поломкой_не_считается(todoist, tmp_path):
    """Версии не ломаются, а выходят: счёт «вышло 14 версий» ничего не диагностирует."""
    from src.inbox import Inbox

    полка = Inbox(tmp_path / "coach.db")
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222", inbox=полка))
    assert полка.сколько() == {}


def test_упавшая_полка_не_отменяет_задачу(todoist, tmp_path):
    """Задача была единственным контуром до полки — она обязана встать и теперь."""
    class Сломанная:
        def поломка(self, *_а, **_к):
            raise sqlite3.OperationalError("database is locked")

    asyncio.run(backstage.raise_task("t", "ритмы", "файл не читается", inbox=Сломанная()))
    assert len(todoist.находки) == 1


# ── общая крыша для технического (просьба владельца 05.08) ───────────────────


def test_техническое_становится_подзадачей_крыши(todoist):
    """Восемь отдельных карточек читались как россыпь дел, хотя дело одно."""
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222"))
    крыша = [з for з in todoist.created if з["content"] == backstage.КРЫША]
    подзадача = [з for з in todoist.created if з["content"] == "Поднять версию движка коуча"]
    assert len(крыша) == 1, "крыша не завелась"
    assert подзадача and подзадача[0].get("parent_id"), "находка не легла под крышу"


def test_у_крыши_нет_ни_даты_ни_метки(todoist):
    """Дата держала бы её в «сегодня» вечно, а метка без даты — в «Потеряшках»,
    у которых норма «пусто»: коуч ругался бы на собственную карточку."""
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222"))
    крыша = [з for з in todoist.created if з["content"] == backstage.КРЫША][0]
    assert "due_string" not in крыша
    assert not крыша.get("labels")


def test_вторая_находка_крышу_не_дублирует(todoist):
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222"))
    asyncio.run(backstage.raise_task("t", "ритмы", "файл не читается"))
    assert len([з for з in todoist.created if з["content"] == backstage.КРЫША]) == 1


def test_разговорное_под_крышу_не_идёт(todoist):
    """Обзор в телеграме и разгребание своих дел — другое место действия."""
    for повод in ("недельный обзор", "завал", "предложения"):
        todoist.created.clear()
        asyncio.run(backstage.raise_task("t", повод, "повод"))
        assert not any(з["content"] == backstage.КРЫША for з in todoist.created), повод
        assert not todoist.находки[0].get("parent_id"), повод
        assert todoist.находки[0].get("project_id"), f"{повод}: потерял проект"


def test_подзадаче_место_задаёт_родитель(todoist):
    """Два указания места у одного факта — два источника правды."""
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222"))
    подзадача = [з for з in todoist.created if з["content"] == "Поднять версию движка коуча"][0]
    assert "project_id" not in подзадача


def test_готовый_текст_остаётся_в_подзадаче(todoist):
    """Владелец копирует описание целиком и отправляет — это и чинит беду."""
    asyncio.run(backstage.raise_task("t", "движок", "вышла 2.1.222"))
    подзадача = [з for з in todoist.created if з["content"] == "Поднять версию движка коуча"][0]
    assert backstage.ШАПКА_АГЕНТУ in подзадача["description"]
