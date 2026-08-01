"""Схемы кнопок для движка: обязательно только то, что обязательно.

Дефект, ради которого написан файл, поймала проверка этапа 14. Короткая
форма схемы SDK — словарь «имя: тип» — выглядит удобной, но делает
**все** поля обязательными. `find_tasks` без `limit` отвечала
«Input validation error: 'limit' is a required property», хотя у поля
есть умолчание. Модель либо получала отказ, либо заполняла десять полей
ради двух нужных.

Тест держит границу: обязательные поля берутся из манифеста, остальные
остаются необязательными, а описания полей едут в саму схему.
"""

from __future__ import annotations

from src.gcal import _schema as calendar_schema
from src.todoist import _schema as todoist_schema
from todoist_mcp import tools as todoist_manifest
from gcal_mcp import tools as calendar_manifest


def test_обязательны_только_помеченные_обязательными():
    add = todoist_manifest.by_name()["add_task"]
    schema = todoist_schema(add.fields)
    assert schema["required"] == ["content"]
    assert "project_id" in schema["properties"]      # поле есть
    assert "project_id" not in schema["required"]    # но не обязательно


def test_кнопка_без_обязательных_полей_не_требует_ничего():
    find = todoist_manifest.by_name()["find_tasks"]
    schema = todoist_schema(find.fields)
    assert "required" not in schema
    assert set(schema["properties"]) == {"filter", "limit"}


def test_описания_полей_едут_в_схему():
    """Раньше их приходилось приклеивать к описанию кнопки — короткая форма
    пояснений не хранит."""
    get = todoist_manifest.by_name()["get_task"]
    schema = todoist_schema(get.fields)
    assert schema["properties"]["task_id"]["description"]


def test_пачка_задач_объявлена_строкой():
    """SDK не принимает список объектов — поле едет строкой и разбирается."""
    bulk = todoist_manifest.by_name()["add_tasks_bulk"]
    schema = todoist_schema(bulk.fields)
    assert schema["properties"]["tasks"]["type"] == "string"
    assert schema["required"] == ["tasks"]


def test_типы_переводятся_в_json_schema():
    upd = todoist_manifest.by_name()["update_task"]
    schema = todoist_schema(upd.fields)
    assert schema["properties"]["priority"]["type"] == "integer"
    assert schema["properties"]["content"]["type"] == "string"
    delete = todoist_manifest.by_name()["delete_task"]
    assert todoist_schema(delete.fields)["properties"]["confirm"]["type"] == "boolean"


def test_у_календаря_та_же_граница():
    create = calendar_manifest.by_name()["create_event"]
    schema = calendar_schema(create.fields)
    assert sorted(schema["required"]) == ["start", "summary"]
    assert "location" in schema["properties"] and "location" not in schema["required"]
    free = calendar_manifest.by_name()["free_slots"]
    assert "required" not in calendar_schema(free.fields)


def test_каждая_кнопка_даёт_годную_схему():
    """Схема без properties или с чужим типом сломала бы кнопку молча."""
    for manifest, build in ((todoist_manifest, todoist_schema),
                            (calendar_manifest, calendar_schema)):
        for t in manifest.TOOLS:
            schema = build(t.fields)
            assert schema["type"] == "object", t.name
            assert set(schema["properties"]) == {f.name for f in t.fields}, t.name
            for name, prop in schema["properties"].items():
                assert prop["type"] in ("string", "integer", "boolean"), (t.name, name)


def test_ни_одна_кнопка_бота_не_объявлена_кириллицей(tmp_path):
    """Кириллица в объявлении кнопки роняет КАЖДЫЙ ответ коуча.

    Имя кнопки движок хотя бы ругает вслух (SEP-986), а кириллические имена
    ПОЛЕЙ принимает молча и потом возвращает ошибочный результат на каждый ход.
    Проверено зондом на сервере 02.08.2026: одна и та же кнопка с полем `слово`
    падает, с полем `word` работает.

    Проверяем ВСЕ собственные серверы бота разом, а не тот, на котором обожглись:
    следующий заведётся по образцу существующих, а питон в этом проекте
    по-русски — соблазн назвать поле по-русски будет каждый раз.
    """
    import asyncio

    from mcp.types import ListToolsRequest

    from src.dashboard_tool import build_dashboard_server
    from src.recall import build_recall_server
    from src.wishes import build_wishes_server

    async def пусто(*a, **к):
        return True

    серверы = {
        "wishes": build_wishes_server(tmp_path, ""),
        "recall": build_recall_server(tmp_path / "coach.db"),
        "dashboard": build_dashboard_server(
            brain_dir=tmp_path, db_path=tmp_path / "coach.db",
            todoist_token="", send=пусто),
    }
    for имя, сервер in серверы.items():
        обработчик = сервер["instance"].request_handlers[ListToolsRequest]
        кнопки = asyncio.run(обработчик(ListToolsRequest(method="tools/list"))).root.tools
        assert кнопки, f"сервер «{имя}» не отдал ни одной кнопки"
        for кнопка in кнопки:
            assert кнопка.name.isascii(), f"{имя}: имя «{кнопка.name}» не латиницей"
            for поле in (кнопка.inputSchema.get("properties") or {}):
                assert поле.isascii(), f"{имя}.{кнопка.name}: поле «{поле}» не латиницей"
