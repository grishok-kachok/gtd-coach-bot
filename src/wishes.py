"""Заявки: «хочу, чтобы ты умел X» — коуч не делает, а записывает.

Решение из PROJECT.md (31.07): бот не меняет логику своей работы. Логика живёт
в плагине, смонтированном только на чтение, — то есть запрет не на честном
слове, а физический. Но мысль, ради которой владелец эту логику хотел поменять,
теряться не должна: за восемь дней жизни бота набралось четыре случая, когда
хотелось «а сделай, чтобы ты…».

Отсюда мост из телеграма к человеку: заявка ложится на полку входящих, а решает он.

**Полка, а не файл в мозге** (этап 20, 05.08.2026). Заявки жили в
`память/журнал/заявки.md`, и «разобрано» означало зачёркивание строки руками.
За неделю накопилось 18 заявок и ни одного зачёркивания — при том что одна была
разобрана и итог записан абзацем ниже. Отметка, которую надо не забыть
поставить, не ставится.

Почему инструмент, а не просьба в конституции. Ровно та же разница, из-за
которой протекло знание: просьба «запиши заявку в такой-то файл» срабатывает,
если модель не отвлеклась и не перепутала формат. Вызов инструмента либо
случился, либо нет, и формат в нём один.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import create_sdk_mcp_server, tool

from .backstage import raise_task
from .inbox import ЗАЯВКА, Inbox

log = logging.getLogger(__name__)


def build_wishes_server(inbox: Inbox, todoist_token: str = "", tz: str = "Europe/Moscow"):
    @tool(
        "record_wish",
        "Записать заявку на новую способность коуча. Звать, когда пользователь просит "
        "то, чего ты не умеешь: «хочу, чтобы ты умел…», «а сделай, чтобы ты…», "
        "«почему ты не…». Свой код ты не правишь — это делает человек, "
        "поэтому заявку надо ЗАПИСАТЬ, а не пообещать и забыть.",
        {"what": str, "why": str},
    )
    async def record_wish(args: dict[str, Any]) -> dict[str, Any]:
        what = (args.get("what") or "").strip()
        if not what:
            return {"content": [{"type": "text", "text": "Пустая заявка — нечего записывать."}]}
        why = (args.get("why") or "").strip()

        today: date = datetime.now(ZoneInfo(tz)).date()
        try:
            await asyncio.to_thread(inbox.положить, ЗАЯВКА, what, зачем=why, день=today)
        except (sqlite3.Error, ValueError):
            log.exception("не смог записать заявку")
            return {"content": [{"type": "text", "text": "Не смог записать заявку — скажи об этом вслух."}]}

        # Копилка, в которую не заглядывают, — свалка. Дёргаем за рукав задачей
        # с датой: одна на копилку, а не на каждую заявку.
        if todoist_token:
            await raise_task(todoist_token, "заявка", f"{today.isoformat()}: «{what}».")

        log.info("заявка записана: %s", what[:80])
        return {"content": [{"type": "text", "text": (
            f"Заявка записана: «{what}». Скажи пользователю, что записал и что "
            "делать это будет человек, а не ты сам."
        )}]}

    return create_sdk_mcp_server(name="wishes", version="1.0.0", tools=[record_wish])
