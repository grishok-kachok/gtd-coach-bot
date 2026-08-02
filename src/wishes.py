"""Заявки: «хочу, чтобы ты умел X» — коуч не делает, а записывает.

Решение из PROJECT.md (31.07): бот не меняет логику своей работы. Логика живёт
в плагине, смонтированном только на чтение, — то есть запрет не на честном
слове, а физический. Но мысль, ради которой владелец эту логику хотел поменять,
теряться не должна: за восемь дней жизни бота набралось четыре случая, когда
хотелось «а сделай, чтобы ты…».

Отсюда мост из телеграма к человеку: заявка ложится в мозг, а решает он.

Почему инструмент, а не просьба в конституции. Ровно та же разница, из-за
которой протекло знание: просьба «запиши заявку в такой-то файл» срабатывает,
если модель не отвлеклась и не перепутала формат. Вызов инструмента либо
случился, либо нет, и формат в нём один.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import create_sdk_mcp_server, tool

from .backstage import raise_task
from .notes import append

log = logging.getLogger(__name__)

WISHES = "заявки.md"

NOTE = (
    "Собирает сам коуч, когда пользователь просит новую способность. Это НЕ обещание "
    "и не задача: код бота меняет человек сам. Разобранную заявку "
    "вычеркни и припиши, чем кончилось."
)


def build_wishes_server(brain_dir: Path, todoist_token: str = "", tz: str = "Europe/Moscow"):
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
        block = f"\n## {today.isoformat()}\n\n- **{what}**\n"
        if why:
            block += f"  Зачем: {why}\n"
        try:
            path = append(
                brain_dir / "память" / "журнал" / WISHES, day=today, block=block,
                title="заявки", heading="Заявки на новые способности коуча",
                note=NOTE, ref="заявки владельца, накопительный список",
                author="агент-коуч (со слов пользователя)", tags="заявка",
            )
        except OSError:
            log.exception("не смог записать заявку")
            return {"content": [{"type": "text", "text": "Не смог записать заявку — скажи об этом вслух."}]}

        # Копилка, в которую не заглядывают, — свалка. Дёргаем за рукав задачей
        # с датой: одна на копилку, а не на каждую заявку.
        if todoist_token:
            await raise_task(todoist_token, "заявка", f"{today.isoformat()}: «{what}».")

        log.info("заявка записана: %s", what[:80])
        return {"content": [{"type": "text", "text": (
            f"Заявка записана в {path.name}: «{what}». Скажи пользователю, что записал и что "
            "делать это будет человек, а не ты сам."
        )}]}

    return create_sdk_mcp_server(name="wishes", version="1.0.0", tools=[record_wish])
