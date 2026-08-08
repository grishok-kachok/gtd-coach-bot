"""Дашборд обязан быть самодостаточным и не падать на пустом мозге.

Два свойства, которые нельзя проверять глазами:

1. **Ноль внешних запросов.** Из РФ половина CDN недоступна — внешний шрифт
   не приедет, и вёрстка развалится ровно на телефоне, где смотреть
   и собирались.
2. **Пустой слой — не поломка.** Слой наполняется разговором, а не этапом.
   Дашборд с пустой миссией должен открываться и приглашать её проговорить.
"""

import asyncio
import re
from datetime import date
from pathlib import Path

import pytest

from src import dashboard, detectors


ВНЕШНЕЕ = re.compile(r"https?://|<script\s+src|<link\s|@import|src=[\"']//")


class ПоддельныйTodoist:
    def __init__(self, labels=None, tasks=None, все=None, проекты=None):
        self.labels = labels or []
        self.tasks = tasks or {}
        # «Потеряшки» и «В игре» фильтром не выражаются — им нужен весь список
        # задач, чтобы увидеть подзадачи. Поэтому у подделки два входа.
        self.все = все or []
        self.проекты = проекты or []

    def __call__(self, token):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_paginated(self, path, params=None, key="results", cap=300):
        # Приборы спрашивают проекты, чтобы спрятать «Архив» (этап 21).
        if path == "/projects":
            return self.проекты
        assert path == "/tasks"
        return list(self.все)

    async def get(self, path, params=None):
        assert path == "/tasks/filter"
        return {"results": self.tasks.get((params or {}).get("query"), [])}


@pytest.fixture
def мозг(tmp_path):
    память = tmp_path / "память"
    (память / "знания").mkdir(parents=True)
    (память / "состояние").mkdir(parents=True)
    (память / "журнал").mkdir(parents=True)
    return tmp_path


def собрать(мозг, todoist, monkeypatch, db=None):
    monkeypatch.setattr(dashboard, "TodoistClient", todoist)
    return asyncio.run(
        dashboard.собрать(мозг, db or Path("/нет/такой.db"), "токен")
    )


def test_пустой_мозг_даёт_страницу_а_не_ошибку(мозг, monkeypatch):
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert "<title>Стратегия · 2026-08-01</title>" in страница
    assert "Пока не заполнено" in страница
    assert "Замеров ещё нет" in страница


def test_ни_одного_внешнего_запроса(мозг, monkeypatch):
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))
    найдено = ВНЕШНЕЕ.findall(страница)
    assert найдено == [], f"страница тянет что-то снаружи: {найдено}"


def test_горизонты_разбираются_по_заголовкам(мозг, monkeypatch):
    (мозг / "память" / "состояние" / "горизонты.md").write_text(
        "---\ntitle: горизонты\n---\n\n# Горизонты\n\n"
        "## ГОД 2026–27\nчетвёртый поток к февралю. Цена: вечера декабря.\n\n"
        "## КВАРТАЛ\nЗакрыть третий поток.\n\n"
        "## МЕСЯЦ\nПереезд.\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)

    assert "четвёртый поток к февралю" in данные.горизонты["ГОД"]
    assert данные.горизонты["КВАРТАЛ"] == "Закрыть третий поток."
    assert "2026–27" in данные.горизонты["ГОД"]  # хвост заголовка не потерян


def test_шапка_заметки_не_попадает_на_страницу(мозг, monkeypatch):
    (мозг / "память" / "знания" / "миссия.md").write_text(
        "---\ntitle: миссия\ntype: знание\n---\n\n# Миссия\n\nБыть отцом и учителем.\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert "Быть отцом и учителем." in страница
    assert "schema_version" not in страница and "type: знание" not in страница


def test_колесо_рисуется_из_ямл_блока(мозг, monkeypatch):
    (мозг / "память" / "журнал" / "колесо-баланса.md").write_text(
        "# Колесо\n\n```yaml\n"
        "2026-07: {работа: 5, семья: 2, здоровье: 3}\n"
        "2026-08: {работа: 4, семья: 3, здоровье: 3}\n"
        "```\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert [м for м, _ in данные.колесо] == ["2026-07", "2026-08"]
    assert "<svg" in страница and "<polygon" in страница
    assert "семья 3" in страница          # подпись последнего замера
    assert '<table class="ряд"' in страница  # линия, а не один кружок


def test_сломанный_ямл_не_роняет_страницу(мозг, monkeypatch):
    (мозг / "память" / "журнал" / "колесо-баланса.md").write_text(
        "```yaml\n2026-08: {работа: 4\n```\n", encoding="utf-8"
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    assert данные.колесо == []
    assert "Замеров ещё нет" in dashboard.нарисовать(данные, date(2026, 8, 1))


def test_приборы_показывают_норму_и_факт(мозг, monkeypatch):
    """Потеряшка — бессрочное дело без метки состояния (этап 21), и считается
    она по всему списку задач, а не фильтром: фильтр не видит подзадач."""
    todoist = ПоддельныйTodoist(все=[
        {"id": "x", "content": "потеряшка", "labels": [], "due": None, "parent_id": None},
    ])
    данные = собрать(мозг, todoist, monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    приборы = dict((имя, факт) for имя, факт, _ in данные.приборы)
    assert приборы["Потеряшки"] == 1 and приборы["Inbox"] == 0
    assert приборы["В игре"] == 0
    assert "красная" in страница and "зелёная" in страница


def test_дашборд_и_обход_считают_потеряшек_одинаково(мозг, monkeypatch):
    """Два отбора одного прибора разошлись бы через месяц — отбор один."""
    задачи = [
        {"id": "a", "content": "потеряшка", "labels": [], "due": None, "parent_id": None},
        {"id": "b", "content": "жду ответа", "labels": ["жду"], "due": None,
         "parent_id": None},
    ]
    данные = собрать(мозг, ПоддельныйTodoist(все=задачи), monkeypatch)
    приборы = dict((имя, факт) for имя, факт, _ in данные.приборы)
    assert приборы["Потеряшки"] == len(detectors.потеряшки_список(задачи)) == 1


def test_файл_называется_по_дате(мозг, monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "TodoistClient", ПоддельныйTodoist())
    путь = asyncio.run(
        dashboard.собрать_файл(мозг, Path("/нет/такой.db"), "токен", куда=tmp_path / "out")
    )
    assert путь.name == f"стратегия-{date.today().isoformat()}.html"
    assert путь.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_разметка_экранируется(мозг, monkeypatch):
    (мозг / "память" / "знания" / "миссия.md").write_text(
        "# Миссия\n\nЖить <script>alert(1)</script> честно.\n", encoding="utf-8"
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))
    assert "<script>alert" not in страница
    assert "&lt;script&gt;" in страница


def test_служебные_врезки_не_едут_на_страницу(мозг, monkeypatch):
    """Поймано вживую 31.07: на телефон вывалилось рассуждение про
    consensus: single вместо миссии. В этом мозге цитата — всегда служебное."""
    (мозг / "память" / "знания" / "миссия.md").write_text(
        "---\ntitle: миссия\nstatus: draft\n---\n\n# Миссия\n\n"
        "> ⚠️ **ЧЕРНОВИК.** Ни одна строка не подтверждена.\n"
        "> Почему `consensus: single` — валидатор считает это занижением.\n\n"
        "Быть отцом и учителем.\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert "Быть отцом и учителем." in страница
    assert "consensus" not in страница and "ЧЕРНОВИК" not in страница


def test_черновик_показан_одной_плашкой(мозг, monkeypatch):
    (мозг / "память" / "знания" / "миссия.md").write_text(
        "---\ntitle: миссия\nstatus: draft\n---\n\n# Миссия\n\nГипотеза.\n", encoding="utf-8"
    )
    (мозг / "память" / "знания" / "ценности.md").write_text(
        "---\ntitle: ценности\nstatus: stable\n---\n\n# Ценности\n\nПять сфер.\n", encoding="utf-8"
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    assert данные.черновики == ["миссия"]
    assert 'class="черновик"' in dashboard.нарисовать(данные, date(2026, 8, 1))


def test_разметка_отрисовывается_а_не_показывается(мозг, monkeypatch):
    """На телефоне были видны сами звёздочки вместо жирного шрифта."""
    (мозг / "память" / "состояние" / "горизонты.md").write_text(
        "# Горизонты\n\n## ГОД\n**четвёртый поток** к февралю.\n\n"
        "- первый пункт\n- второй `код`\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert "<strong>четвёртый поток</strong>" in страница
    assert "<li>первый пункт</li>" in страница
    assert "<code>код</code>" in страница
    assert "**" not in страница


def test_на_витрину_едет_только_суть_а_не_весь_файл(мозг, monkeypatch):
    """Владелец открыл файл на телефоне и увидел таблицу «откуда взято»
    и раздел «что спросить». Дашборд — витрина, а не читалка заметок."""
    (мозг / "память" / "знания" / "миссия.md").write_text(
        "---\ntitle: миссия\n---\n\n# Миссия\n\n"
        "## Гипотеза одним абзацем\n\nБыть отцом и учителем.\n\n"
        "## На чём это построено\n\n| Кусок | Откуда |\n|---|---|\n| роль | 20.07 |\n\n"
        "## Что спросить\n\n1. Что должно быть правдой через десять лет?\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert "Быть отцом и учителем." in страница
    assert "Откуда" not in страница
    assert "Что спросить" not in страница
    assert "##" not in страница


def test_таблица_отрисовывается_таблицей(мозг, monkeypatch):
    (мозг / "память" / "состояние" / "горизонты.md").write_text(
        "# Горизонты\n\n## ГОД\n| Цель | Цена |\n|---|---|\n| четвёртый поток | вечера |\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert "<th>Цель</th>" in страница and "<td>вечера</td>" in страница
    assert "|---|" not in страница


def test_одиночный_перенос_не_рвёт_предложение(мозг, monkeypatch):
    (мозг / "память" / "состояние" / "горизонты.md").write_text(
        "# Горизонты\n\n## МЕСЯЦ\nДовести поток\nдо конца августа.\n", encoding="utf-8"
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))
    assert "Довести поток до конца августа." in страница
    assert "<br>" not in страница


def test_прибор_без_сферы_считает_тем_же_кодом(мозг, monkeypatch):
    """Панель и ночной обход обязаны показывать одно число: считает их
    одна функция, а не две похожие."""

    class Todoist(ПоддельныйTodoist):
        async def get_paginated(self, path, params=None, key="results", cap=300):
            if path == "/projects":
                return []
            return [
                {"id": "a", "content": "голая", "labels": [], "due": None},
                {"id": "b", "content": "одетая", "labels": ["☸️быт"], "due": None},
            ]

    данные = собрать(мозг, Todoist(), monkeypatch)
    assert dict((и, ф) for и, ф, _ in данные.приборы)["Без сферы"] == 1


def _база_с_закрытыми(tmp_path, строки):
    """Мини-снимок закрытых задач: ровно те поля, которые читает дашборд."""
    import sqlite3
    путь = tmp_path / "coach.db"
    with sqlite3.connect(путь) as db:
        db.execute("CREATE TABLE todoist_closed (task_id TEXT, completed_at TEXT, labels TEXT)")
        db.executemany("INSERT INTO todoist_closed VALUES (?,?,?)", строки)
    return путь


def test_дела_по_сферам_считаются_по_меткам_закрытых(мозг, monkeypatch, tmp_path):
    """Прибор считает ЗАКРЫТОЕ: метка на живой задаче сюда не попадает,
    и наоборот — закрытая задача без метки не должна попасть никуда."""
    сегодня = date.today().isoformat()
    база = _база_с_закрытыми(tmp_path, [
        ("1", сегодня, "☸️работа,⏱️S"),
        ("2", сегодня, "☸️работа"),
        ("3", сегодня, "☸️семья"),
        ("4", сегодня, ""),
        ("5", "2020-01-01", "☸️работа"),   # старше окна — не считается
    ])
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch, db=база)
    счёт = dict(данные.сферы)
    assert счёт["☸️работа"] == 2, "две закрытые за окном, третья слишком старая"
    assert счёт["☸️семья"] == 1
    assert счёт["☸️быт"] == 0, "сфера без дел — это тоже показание, а не пропуск"
    assert len(данные.сферы) == 6, "все шесть всегда на месте"


def test_нет_базы_нет_паники(мозг, monkeypatch):
    """У ученика на первом запуске снимков нет — шесть нулей, а не ошибка."""
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    assert [n for _, n in данные.сферы] == [0] * 6


def test_подписи_сфер_без_значка_как_у_колеса(мозг, monkeypatch):
    """Колесо подписано словами из `ценности.md` («работа»), а метки несут
    приставку `☸️`. Рядом со значком и без него — это два разных списка,
    то есть ровно та болезнь, которую этап лечил."""
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    данные.сферы = [("☸️работа", 3), ("☸️семья", 1)]
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))
    assert "<th>работа</th>" in страница
    assert "<th>☸️работа</th>" not in страница


def test_дела_по_сферам_рисуются_рядом_с_колесом(мозг, monkeypatch):
    """Предмет разговора на стратсессии — расхождение самочувствия и факта,
    а увидеть его можно только рядом."""
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))
    assert "Колесо баланса — самочувствие" in страница
    assert "Дела по сферам" in страница
    assert "Работа по целям" not in страница, "мёртвый блок целей снят"


def test_дашборд_прячет_архив_так_же_как_обход(мозг, monkeypatch):
    """Два места, где считаются потеряшки, обязаны прятать одно и то же."""
    todoist = ПоддельныйTodoist(
        проекты=[{"id": "арх", "name": "Архив"}, {"id": "жив", "name": "Личное"}],
        все=[
            {"id": "a", "content": "живая потеряшка", "labels": [], "due": None,
             "parent_id": None, "project_id": "жив"},
            {"id": "b", "content": "мусор из архива", "labels": [], "due": None,
             "parent_id": None, "project_id": "арх"},
        ],
    )
    данные = собрать(мозг, todoist, monkeypatch)
    приборы = dict((имя, факт) for имя, факт, _ in данные.приборы)
    assert приборы["Потеряшки"] == 1, "архивный мусор попал в потеряшки"


# --- окна периодов (этап 18) ---


def test_у_каждой_стратсессии_своё_окно():
    """До этапа 18 окон было два: всё, что не начиналось на «мес», молча
    превращалось в неделю. С появлением квартальной и годовой это значило бы
    разговор про три месяца по семидневным числам."""
    from src.dashboard_tool import _окно

    assert _окно("неделя")[0] == 7
    assert _окно("месяц")[0] == 30
    assert _окно("квартал")[0] == 90
    assert _окно("год")[0] == 365


def test_окно_узнаётся_по_началу_слова():
    """Коуч может сказать «месячная» или «мес» — это одно и то же окно."""
    from src.dashboard_tool import _окно

    assert _окно("месячная")[0] == _окно("мес")[0] == 30
    assert _окно("квартальная")[0] == 90
    assert _окно("годовая")[0] == 365


def test_незнакомый_период_даёт_неделю_но_со_следом(caplog):
    """Подмена осталась (что-то присылать надо), но перестала быть немой."""
    import logging

    from src.dashboard_tool import _окно

    with caplog.at_level(logging.WARNING):
        дней, подпись = _окно("пятилетка")
    assert дней == 7 and "неделю" in подпись
    assert any("пятилетка" in з.getMessage() for з in caplog.records), "подмена прошла молча"


def test_подписи_разные_у_всех_четырёх():
    """Одинаковая подпись на разных файлах — способ перепутать их в чате."""
    from src.dashboard_tool import ОКНА

    подписи = [подпись for _, _, подпись in ОКНА]
    assert len(подписи) == len(set(подписи)), подписи


def test_проводка_окна_а_не_только_функция(tmp_path, monkeypatch):
    """Проверяем не `_окно`, а то, что её кто-то зовёт.

    Поймано третьим циклом приёмки этапа 18: тесты выше проверяли саму функцию
    и оставались зелёными, если бы `send_dashboard` про неё забыл. Тот же
    признак, что 06.08 стоил кнопки стратсессии: тест звал метод напрямую
    и не видел, что проводка оборвана."""
    import asyncio

    from src import dashboard_tool

    поймано = {}

    async def поддельный_сбор(brain_dir, db_path, token, куда=None, дней=7):
        поймано["дней"] = дней
        путь = tmp_path / "дашборд.html"
        путь.write_text("<html></html>", encoding="utf-8")
        return путь

    async def поддельная_отправка(путь, подпись):
        поймано["подпись"] = подпись
        return True

    from mcp.types import CallToolRequest, CallToolRequestParams

    monkeypatch.setattr(dashboard_tool, "собрать_файл", поддельный_сбор)
    сервер = dashboard_tool.build_dashboard_server(
        brain_dir=tmp_path, db_path=tmp_path / "coach.db",
        todoist_token="t", send=поддельная_отправка)
    обработчик = сервер["instance"].request_handlers[CallToolRequest]

    def позвать(период):
        return asyncio.run(обработчик(CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name="send_dashboard",
                                         arguments={"period": период}))))

    for период, ждём_дней in (("квартал", 90), ("год", 365), ("месяц", 30), ("неделя", 7)):
        поймано.clear()
        позвать(период)
        assert поймано["дней"] == ждём_дней, (
            f"period={период}: собрали за {поймано['дней']} дн. вместо {ждём_дней}")
        assert период[:3] in поймано["подпись"].lower(), поймано["подпись"]
