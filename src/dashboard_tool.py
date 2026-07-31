"""Инструмент «прислать дашборд» — потому что отправка обязана случаться.

Недельный обзор и месячный итог **без файла не считаются проведёнными**
(решение владельца 31.07). А то, что обязано случаться, делается инструментом,
а не просьбой в тексте: просьба срабатывает, если модель не отвлеклась;
вызов инструмента либо случился, либо нет.

Отсюда же форма ответа: инструмент возвращает коучу, что файл ушёл, — и коуч
об этом говорит вслух. Молча отправленный файл неотличим от неотправленного.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .dashboard import собрать_файл

log = logging.getLogger(__name__)


def build_dashboard_server(
    brain_dir: Path,
    db_path: Path,
    todoist_token: str,
    send: Callable[[Path, str], Awaitable[bool]],
):
    """`send(путь, подпись)` — как файл уходит владельцу. Возвращает, дошёл ли."""

    @tool(
        "send_dashboard",
        "Собрать дашборд курса и прислать Василию файлом в телеграм. Звать "
        "на недельном обзоре и месячном итоге — там это ОБЯЗАТЕЛЬНАЯ часть "
        "ритуала, без файла обзор не считается проведённым, — а также когда "
        "Василий просит «покажи картину» или «где я сейчас». Файл открывается "
        "прямо в телеграме, на телефоне и на компьютере.",
        {"period": str},
    )
    async def send_dashboard(args: dict[str, Any]) -> dict[str, Any]:
        период = (args.get("period") or "неделя").strip().lower()
        дней = 30 if период.startswith("мес") else 7
        подпись = "Курс за месяц" if дней == 30 else "Курс за неделю"

        try:
            путь = await собрать_файл(brain_dir, db_path, todoist_token, дней=дней)
        except OSError:
            log.exception("дашборд не собрался")
            return {"content": [{"type": "text", "text": (
                "Дашборд не собрался — скажи Василию вслух, что файла не будет, "
                "и разбери обзор словами."
            )}]}

        if not await send(путь, подпись):
            return {"content": [{"type": "text", "text": (
                "Файл собрался, но не ушёл в телеграм. Скажи об этом вслух — "
                "ритуал без файла не считается проведённым."
            )}]}

        размер = путь.stat().st_size
        log.info("дашборд отправлен: %s (%d Б)", путь.name, размер)
        return {"content": [{"type": "text", "text": (
            f"Дашборд «{путь.name}» отправлен ({размер // 1024} КБ). "
            "Скажи Василию, что файл пришёл и его можно открыть прямо здесь."
        )}]}

    return create_sdk_mcp_server(name="dashboard", version="1.0.0", tools=[send_dashboard])
