"""Кнопки разбора предложений: что коуч видит и чем закрывает.

Проверяется то, что ломается молча: имена полей латиницей (кириллические
движок принимает и потом ошибается на каждом ходу), необязательный `limit`
и отказ закрыть запись без итога — иначе «разобрано» опять станет отметкой,
которую забывают поставить.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from src.inbox import ПРЕДЛОЖЕНИЕ, ПРОМАХ, Inbox
from src.inbox_tool import build_inbox_server


@pytest.fixture
def полка(tmp_path):
    return Inbox(tmp_path / "coach.db")


def _собрать(сервер) -> dict:
    """Достать обработчики из sdk-сервера, как это делает движок."""
    from mcp.types import ListToolsRequest

    instance = сервер["instance"]
    listed = asyncio.run(
        instance.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    ).root.tools
    return {t.name: t for t in listed}


def позвать(сервер, имя, аргументы):
    from mcp.types import CallToolRequest, CallToolRequestParams

    instance = сервер["instance"]
    запрос = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name=имя, arguments=аргументы)
    )
    ответ = asyncio.run(instance.request_handlers[CallToolRequest](запрос))
    return "\n".join(ч.text for ч in ответ.root.content)


def test_имена_кнопок_и_полей_латиницей(полка):
    """Кириллическое имя поля движок принимает молча и потом немеет на каждом ходу."""
    объявленные = _собрать(build_inbox_server(полка))
    assert set(объявленные) == {"show_memory_proposals", "close_memory_proposal"}
    for кнопка in объявленные.values():
        for поле in кнопка.inputSchema.get("properties", {}):
            assert поле.isascii(), f"поле «{поле}» не латиницей — движок ошибётся молча"


def test_лимит_необязателен(полка):
    """Короткая форма схемы делает все поля обязательными — здесь так нельзя."""
    показать = _собрать(build_inbox_server(полка))["show_memory_proposals"]
    assert "required" not in показать.inputSchema


def test_итог_и_номер_обязательны(полка):
    закрыть = _собрать(build_inbox_server(полка))["close_memory_proposal"]
    assert set(закрыть.inputSchema["required"]) == {"item_id", "outcome", "result"}
    assert "return_on" not in закрыть.inputSchema["required"], "дата нужна только отложенной"


def test_показывает_только_предложения(полка):
    """Заявки, промахи и поломки коуч не разбирает — их чинит правкой кода человек."""
    полка.положить(ПРЕДЛОЖЕНИЕ, "вылет 17 сентября", зачем="со слов пользователя")
    полка.положить(ПРОМАХ, "полез в архив за записанным")
    сервер = build_inbox_server(полка)
    ответ = позвать(сервер, "show_memory_proposals", {})
    assert "вылет 17 сентября" in ответ
    assert "полез в архив" not in ответ


def test_видно_откуда_запись(полка):
    """Граница «факт пишем сразу, вывод — после подтверждения» обязана быть видна."""
    полка.положить(ПРЕДЛОЖЕНИЕ, "ставки заводят", зачем="вывод коуча — сначала спросить")
    ответ = позвать(build_inbox_server(полка), "show_memory_proposals", {})
    assert "вывод коуча" in ответ


def test_пустая_копилка_это_ответ(полка):
    ответ = позвать(build_inbox_server(полка), "show_memory_proposals", {})
    assert "нет" in ответ.lower()
    assert "не выдумывай" in ответ


def test_закрытие_убирает_запись_из_списка(полка):
    id_ = полка.положить(ПРЕДЛОЖЕНИЕ, "вылет 17 сентября")
    сервер = build_inbox_server(полка)
    ответ = позвать(сервер, "close_memory_proposal", {
        "item_id": id_, "outcome": "сделана", "result": "записал в состояние/горизонты",
    })
    assert "закрыта" in ответ
    assert полка.открытые(ПРЕДЛОЖЕНИЕ) == []


def test_без_итога_не_закрывает(полка):
    """Отклонённое без причины через месяц предложат ровно то же самое."""
    id_ = полка.положить(ПРЕДЛОЖЕНИЕ, "спорный вывод")
    ответ = позвать(build_inbox_server(полка), "close_memory_proposal", {
        "item_id": id_, "outcome": "отклонена", "result": "  ",
    })
    assert "Не закрыл" in ответ
    assert len(полка.открытые(ПРЕДЛОЖЕНИЕ)) == 1


def test_отложенная_без_даты_не_закрывается(полка):
    id_ = полка.положить(ПРЕДЛОЖЕНИЕ, "потом обсудим")
    ответ = позвать(build_inbox_server(полка), "close_memory_proposal", {
        "item_id": id_, "outcome": "отложена", "result": "не сейчас",
    })
    assert "Не закрыл" in ответ

    ответ = позвать(build_inbox_server(полка), "close_memory_proposal", {
        "item_id": id_, "outcome": "отложена", "result": "не сейчас",
        "return_on": "2026-09-20",
    })
    assert "закрыта" in ответ


def test_непонятный_исход_отклоняется(полка):
    id_ = полка.положить(ПРЕДЛОЖЕНИЕ, "что-то")
    ответ = позвать(build_inbox_server(полка), "close_memory_proposal", {
        "item_id": id_, "outcome": "потом", "result": "итог",
    })
    assert "Не понял исход" in ответ


def test_чужой_номер_не_роняет_разговор(полка):
    ответ = позвать(build_inbox_server(полка), "close_memory_proposal", {
        "item_id": 999, "outcome": "сделана", "result": "итог",
    })
    assert "нет" in ответ.lower()


def test_старое_показывается_первым(полка):
    """Разбирают с самого залежавшегося, а не с самого свежего."""
    полка.положить(ПРЕДЛОЖЕНИЕ, "свежее", день=date(2026, 8, 4))
    полка.положить(ПРЕДЛОЖЕНИЕ, "залежалось", день=date(2026, 7, 1))
    ответ = позвать(build_inbox_server(полка), "show_memory_proposals", {})
    assert ответ.index("залежалось") < ответ.index("свежее")
