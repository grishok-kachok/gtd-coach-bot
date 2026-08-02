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

from src import dashboard


ВНЕШНЕЕ = re.compile(r"https?://|<script\s+src|<link\s|@import|src=[\"']//")


class ПоддельныйTodoist:
    def __init__(self, labels=None, tasks=None):
        self.labels = labels or []
        self.tasks = tasks or {}

    def __call__(self, token):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_paginated(self, path, params=None, key="results", cap=300):
        assert path == "/tasks"
        return [
            {"id": f"t{i}", "content": з.get("content", ""), "labels": [м],
             "due": з.get("due")}
            for м, задачи in self.tasks.items() if м.startswith("@цель-")
            for i, з in enumerate(задачи)
        ]

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
        "## ГОД 2026–27\n6 поток к февралю. Цена: вечера декабря.\n\n"
        "## КВАРТАЛ\nЗакрыть 5 поток.\n\n"
        "## МЕСЯЦ\nПереезд.\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)

    assert "6 поток к февралю" in данные.горизонты["ГОД"]
    assert данные.горизонты["КВАРТАЛ"] == "Закрыть 5 поток."
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
    todoist = ПоддельныйTodoist(tasks={"@актив & no date": [{"content": "потеряшка"}]})
    данные = собрать(мозг, todoist, monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    приборы = dict((имя, факт) for имя, факт, _ in данные.приборы)
    assert приборы["Потеряшки"] == 1 and приборы["Inbox"] == 0
    assert "красная" in страница and "зелёная" in страница


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
        "# Горизонты\n\n## ГОД\n**6 поток** к февралю.\n\n"
        "- первый пункт\n- второй `код`\n",
        encoding="utf-8",
    )
    данные = собрать(мозг, ПоддельныйTodoist(), monkeypatch)
    страница = dashboard.нарисовать(данные, date(2026, 8, 1))

    assert "<strong>6 поток</strong>" in страница
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
        "# Горизонты\n\n## ГОД\n| Цель | Цена |\n|---|---|\n| 6 поток | вечера |\n",
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


def test_работа_по_цели_это_карточка_плюс_подзадачи(мозг, monkeypatch):
    """Метка на подзадачи не наследуется, а карточка-замысел по методологии
    без даты. Считать только помеченное — значит объявить спящей цель «Лиссабон»
    с тридцатью задачами (проверено на живом аккаунте 31.07)."""

    class Todoist(ПоддельныйTodoist):
        async def get_paginated(self, path, params=None, key="results", cap=300):
            assert path == "/tasks"
            return [
                {"id": "к", "content": "* ✈️ Лиссабон", "labels": ["цель-лиссабон"], "due": None},
                {"id": "п1", "content": "Визы", "parent_id": "к", "labels": [],
                 "due": {"date": "2026-09-01"}},
                {"id": "в1", "content": "Фото на визу", "parent_id": "п1", "labels": [],
                 "due": {"date": "2026-08-20"}},
                {"id": "чужая", "content": "Купить лампочку", "labels": [], "due": None},
            ]

    данные = собрать(мозг, Todoist(), monkeypatch)
    assert данные.цели == [
        {"имя": "лиссабон", "всего": 3, "с_датой": 2, "ближайший": "2026-08-20"}
    ]

    страница = dashboard.нарисовать(данные, date(2026, 8, 1))
    assert "— спит" not in страница, "цель с датами не может быть спящей"
    assert dict((и, ф) for и, ф, _ in данные.приборы)["Спящие цели"] == 0


def test_спящей_считается_цель_без_единой_даты(мозг, monkeypatch):
    class Todoist(ПоддельныйTodoist):
        async def get_paginated(self, path, params=None, key="results", cap=300):
            return [
                {"id": "к", "content": "* 🚀 6 поток", "labels": ["цель-6поток"], "due": None},
                {"id": "п", "content": "Программа", "parent_id": "к", "labels": [], "due": None},
            ]

    данные = собрать(мозг, Todoist(), monkeypatch)
    assert данные.цели[0]["с_датой"] == 0
    assert dict((и, ф) for и, ф, _ in данные.приборы)["Спящие цели"] == 1
    assert "— спит" in dashboard.нарисовать(данные, date(2026, 8, 1))
