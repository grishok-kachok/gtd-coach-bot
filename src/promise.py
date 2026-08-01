"""Обещание дня против факта: единственное место этапа, где ночью думает модель.

Утренний чек-ин заканчивается обещанием — одним делом, которое человек назвал
главным. Сдержал он его или закрыл вместо него пять других — по базе не видно:
обещание живёт словами в разговоре, а не полем в Todoist. Значит нужен разбор
текста, а текст читает модель.

Почему это всё-таки не «пусть модель оценивает» в дурном смысле. Модель здесь
не судит человека и не делает выводов — она достаёт из разговора два факта:
какое дело было названо и нашлось ли оно среди закрытых. Приговор («ты берёшь
больше, чем тянешь») — это уже вывод, и он попадает в память только через
подтверждение, как всё остальное.

Счёт копится в базе, а не в файлах памяти: дом цифр — база, дом смысла —
журнал. За месяц из этих строк получается показатель «попадает в обещание
N раз из десяти», и вот он уже меняет поведение коуча — сколько обещаний
давать брать за раз.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .prompts import load as load_prompt

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_promise (
    day       TEXT PRIMARY KEY,
    обещание  TEXT NOT NULL DEFAULT '',
    исход     TEXT NOT NULL DEFAULT '',   -- выполнено | частично | нет | неизвестно
    главное   TEXT NOT NULL DEFAULT ''    -- да | другое | нет данных
);
"""

ИСХОДЫ = ("выполнено", "частично", "нет", "неизвестно")


class PromiseWatch:
    """Ночной разбор: было ли обещание дня и случилось ли оно."""

    def __init__(self, db_path: Path, model: str = "claude-fable-5") -> None:
        self.db_path = db_path
        self.model = model
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.executescript(SCHEMA)

    async def _ask(self, prompt: str, system: str) -> str:
        options = ClaudeAgentOptions(
            model=self.model,
            effort="high",  # раз в сутки — экономить нечего
            tools=[],       # думать, а не лазить по файлам
            system_prompt=system,
        )
        parts: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage) and message.is_error:
                log.error("разбор обещания не удался: %s", message.result)
        return "\n".join(p.strip() for p in parts if p.strip()).strip()

    def _закрытые(self, day: date) -> list[str]:
        with sqlite3.connect(self.db_path, timeout=30) as db:
            строки = db.execute(
                "SELECT content FROM todoist_closed WHERE completed_at LIKE ?",
                (f"{day.isoformat()}%",),
            ).fetchall()
        return [content for (content,) in строки]

    @staticmethod
    def _поле(ответ: str, имя: str) -> str:
        """Достать строку ответа. Разбор нарочно тупой: модель отвечает по форме."""
        for строка in ответ.splitlines():
            голова, _, хвост = строка.partition(":")
            if голова.strip().upper() == имя:
                return хвост.strip()
        return ""

    def _записать(self, day: date, обещание: str, исход: str, главное: str) -> None:
        with sqlite3.connect(self.db_path, timeout=30) as db:
            db.execute(
                "INSERT INTO day_promise(day, обещание, исход, главное) VALUES(?,?,?,?)"
                " ON CONFLICT(day) DO UPDATE SET обещание = excluded.обещание,"
                " исход = excluded.исход, главное = excluded.главное",
                (day.isoformat(), обещание, исход, главное),
            )

    async def run(self, day: date, transcript: str) -> dict[str, str]:
        """Разобрать день. Пустой разговор — нечего разбирать, и это не ошибка."""
        if len(transcript) < 200:
            return {}
        закрытые = self._закрытые(day)
        prompt = load_prompt("обещание-дня")
        ответ = await self._ask(
            prompt.format(
                day=day.isoformat(), transcript=transcript,
                closed_count=len(закрытые),
                closed="\n".join(f"- {c}" for c in закрытые) or "- ничего",
            ),
            prompt.system,
        )
        обещание = self._поле(ответ, "ОБЕЩАНИЕ")
        исход = self._поле(ответ, "ИСХОД").lower()
        главное = self._поле(ответ, "ГЛАВНОЕ").lower()
        if исход not in ИСХОДЫ:
            # Форма не соблюдена — записать «выполнено» наугад хуже, чем не записать:
            # испорченная строка тихо въедет в статистику и будет там жить.
            log.warning("разбор обещания за %s не по форме: %r", day, ответ[:200])
            return {}

        # «Обещания не было» — это пустая строка, а не слово «нет».
        # Первый живой прогон записал обещание «нет» с исходом «нет»: день,
        # в который человек ничего не обещал, попал в счёт как несдержанный.
        # Статистика «сдержано 0 из 5» получилась бы из четырёх пустых дней.
        if обещание.strip().lower().strip(".") in ("", "нет", "не было", "-", "—"):
            обещание, исход, главное = "", "неизвестно", "нет данных"
        self._записать(day, обещание, исход, главное)
        log.info("обещание за %s: %s → %s", day, обещание[:40] or "не давал", исход)
        return {"обещание": обещание, "исход": исход, "главное": главное}
