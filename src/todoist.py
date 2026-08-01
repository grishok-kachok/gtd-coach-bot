"""Инструменты Todoist для движка бота — обёртка над манифестом кнопок.

Здесь не объявлено ни одной кнопки. Список, описания и поля живут в манифесте
пакета `todoist-mcp` (`coach_todoist_mcp.tools`), логика — в его же `core.py`.
Этот файл только разворачивает манифест в инструменты Claude Agent SDK.

Почему обёрток всё-таки две. Движки принимают разные типы: MCP-сервер умеет
поле-список объектов, SDK — только плоские (`str`, `int`, `bool`). Поэтому
пачка задач приезжает сюда JSON-строкой и разбирается на месте. Разница
названа в манифесте (`Field.kind == "json"`) и раскрывается здесь; всё
остальное — общее.

До 01.08.2026 обёртки объявляли кнопки каждая по-своему и уже разошлись:
`update_task` здесь знал про дедлайн, а в MCP-сервере нет. Теперь разойтись
нечему.

Один дом кода: пакет не копируется в этот репозиторий — он приезжает
отдельным репо и монтируется в контейнер (см. docker-compose.yml).
Egress к Todoist из РФ — через Xray-мост: TodoistClient(trust_env=True) сам
подхватывает HTTPS_PROXY из окружения контейнера.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from coach_todoist_mcp import plan as plan_module
from coach_todoist_mcp import tools as manifest
from coach_todoist_mcp.client import TodoistClient, TodoistError

log = logging.getLogger(__name__)

# Типы, которые понимает схема SDK. Список объектов сюда не помещается,
# поэтому приезжает строкой — см. _unpack.
SDK_TYPES: dict[str, Any] = {"str": str, "int": int, "bool": bool, "json": str}


def _describe(tool_def: manifest.Tool, fields: tuple[manifest.Field, ...]) -> str:
    """Описание кнопки плюс расшифровка полей.

    Схема SDK хранит только типы, без пояснений, поэтому «что значит
    parent_id» приходится говорить в тексте — иначе модель угадывает.
    """
    described = [f"{f.name} — {f.description}" for f in fields if f.description]
    if not described:
        return tool_def.description
    return tool_def.description + " Поля: " + "; ".join(described) + "."


def _unpack(tool_def: manifest.Tool, args: dict[str, Any]) -> dict[str, Any] | str:
    """Привести пришедшие поля к тому виду, которого ждёт манифест.

    Возвращает либо готовые аргументы, либо строку с объяснением ошибки —
    её показываем модели вместо падения.
    """
    prepared = dict(args)
    for f in tool_def.fields:
        if f.kind != "json" or f.name not in prepared:
            continue
        raw = prepared.get(f.name)
        if isinstance(raw, (list, dict)) or raw in (None, ""):
            continue
        try:
            prepared[f.name] = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as err:
            return f"Поле «{f.name}» не разобрано как JSON: {err}"
        if not isinstance(prepared[f.name], list):
            return f"Поле «{f.name}» должно быть JSON-массивом объектов."
    return prepared


def build_todoist_server(token: str, switches: dict[str, bool] | None = None):
    """Собрать MCP-сервер движка из манифеста: доступное на тарифе и включённое."""

    def ok(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    current_plan = plan_module.current(token)
    built = []

    for tool_def in manifest.selected(current_plan, switches):
        fields = tool_def.visible_fields(current_plan)
        schema = {f.name: SDK_TYPES.get(f.kind, str) for f in fields}

        def make(tool_def: manifest.Tool = tool_def):
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                prepared = _unpack(tool_def, args)
                if isinstance(prepared, str):
                    return ok(prepared)
                try:
                    async with TodoistClient(token) as client:
                        return ok(await tool_def.run(client, prepared))
                except TodoistError as err:
                    # Ошибку отдаём словами, а не бросаем: модель должна её
                    # увидеть и объяснить, а не молча потерять действие.
                    log.warning("Todoist: %s", err)
                    return ok(f"Не получилось (Todoist): {err}")
            return handler

        built.append(
            tool(tool_def.name, _describe(tool_def, fields), schema)(make())
        )

    log.info(
        "Todoist: кнопок %d, тариф %s (%s)",
        len(built), current_plan.name, current_plan.source,
    )
    return create_sdk_mcp_server(name="todoist", version="3.0.0", tools=built)
