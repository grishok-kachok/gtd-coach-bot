"""Инструменты Календаря для движка бота — обёртка над манифестом пакета.

Здесь не объявлено ни одной кнопки. Список, описания и поля живут в манифесте
пакета `gcal-mcp` (`gcal_mcp.tools`), логика — в его `core.py`, доступ —
в его `client.py`. Этот файл только разворачивает манифест в инструменты
Claude Agent SDK.

До 01.08.2026 календарь существовал в полутора экземплярах: у бота свой
питон на 243 строки, а в сессии за компьютером его не было вообще. Теперь
код один, и оба канала берут его оттуда.

Один дом кода: пакет не копируется в этот репозиторий — он приезжает
отдельным репо и монтируется в контейнер (см. docker-compose.yml).
Выход к Google из РФ блокируется, поэтому трафик идёт через прокси-мост —
тот же, что у распознавания речи.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from gcal_mcp import tools as manifest
from gcal_mcp.client import CalendarClient, CalendarError

log = logging.getLogger(__name__)

SDK_TYPES: dict[str, Any] = {"str": str, "int": int, "bool": bool}


def _describe(tool_def: manifest.Tool) -> str:
    """Описание кнопки плюс расшифровка полей.

    Схема SDK хранит только типы, без пояснений, поэтому «что значит
    calendars» приходится говорить в тексте — иначе модель угадывает.
    """
    described = [f"{f.name} — {f.description}" for f in tool_def.fields if f.description]
    if not described:
        return tool_def.description
    return tool_def.description + " Поля: " + "; ".join(described) + "."


def build_calendar_server(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    calendar_id: str = "primary",
    tz: str = "Europe/Moscow",
    proxy: str | None = None,
    switches: dict[str, bool] | None = None,
):
    """Собрать MCP-сервер движка из манифеста: включённые кнопки Календаря."""

    def ok(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    client = CalendarClient(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        calendar_id=calendar_id,
        tz=tz,
        proxy=proxy,
    )

    built = []
    for tool_def in manifest.selected(switches):
        schema = {f.name: SDK_TYPES.get(f.kind, str) for f in tool_def.fields}

        def make(tool_def: manifest.Tool = tool_def):
            async def handler(args: dict[str, Any]) -> dict[str, Any]:
                try:
                    return ok(await tool_def.run(client, args))
                except CalendarError as err:
                    # Ошибку отдаём словами, а не бросаем: модель должна её
                    # увидеть и объяснить, а не молча потерять действие.
                    log.warning("Календарь: %s", err)
                    return ok(f"Не получилось (Календарь): {err}")
            return handler

        built.append(tool(tool_def.name, _describe(tool_def), schema)(make()))

    log.info("Календарь: кнопок %d", len(built))
    return create_sdk_mcp_server(name="calendar", version="2.0.0", tools=built)
