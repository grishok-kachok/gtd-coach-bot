"""Ночная выжимка: разговор дня превращается в короткую запись в журнале.

Смысл — не ждать, пока движок сам сожмёт разговор при упоре в лимит (тогда
он режет вслепую и теряет мелочи), а раз в сутки перечитать день целиком самой
умной моделью и оставить плотный конспект. Дальше конспекты сами укрупняются:
дни → недели → месяцы. Так память за месяцы влезает в любой разговор.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    get_session_messages,
    query,
)

log = logging.getLogger(__name__)

DAY_PROMPT = """Ниже — стенограмма сегодняшнего разговора коуча с Василием.

Сделай плотную выжимку дня. Требования:
- Что Василий делал, что решил, о чём договорились, что его беспокоит.
- Обязательно сохрани конкретику: имена, суммы, даты, названия задач и проектов.
- Отдельной строкой — что изменилось в планах или сроках, если менялось.
- Отдельной строкой — новое понимание про Василия (что сработало, что бесит), если проявилось.
- Не пересказывай вежливости и служебную возню с инструментами.
- Пиши сжато, но не ценой фактов: лучше лишняя строка, чем потерянная деталь.
- Простой текст, без markdown-заголовков. 10–20 строк.

Если разговора по сути не было — ответь одной строкой: ПУСТО

Стенограмма:

"""

ROLLUP_PROMPT = """Ниже — выжимки за период ({period}). Сделай из них одну обобщённую выжимку.

- Убери повседневный шум, оставь линии: что двигалось, что застряло, что решили.
- Сохрани конкретику по срокам, деньгам, договорённостям и результатам.
- Отдельно — устойчивые наблюдения про Василия (паттерны, а не разовые эпизоды).
- Простой текст, без markdown-заголовков. 15–30 строк.

Выжимки:

"""


@dataclass
class DigestPaths:
    days: Path
    weeks: Path
    months: Path

    @classmethod
    def under(cls, brain_dir: Path) -> "DigestPaths":
        base = brain_dir / "память" / "журнал" / "выжимки"
        return cls(days=base / "дни", weeks=base / "недели", months=base / "месяцы")


class Digester:
    def __init__(self, brain_dir: Path, model: str = "claude-fable-5") -> None:
        self.brain_dir = brain_dir
        self.model = model
        self.paths = DigestPaths.under(brain_dir)

    async def _summarize(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=self.model,
            effort="high",  # выжимка делается раз в сутки — экономить тут нечего
            tools=[],       # думать, а не лазить по файлам
            system_prompt=(
                "Ты ведёшь дневник коуча. Твоя работа — сжимать разговоры, "
                "не теряя фактов. Пишешь по-русски, просто и плотно."
            ),
        )
        parts: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage) and message.is_error:
                log.error("выжимка не удалась: %s", message.result)
        return "\n".join(p.strip() for p in parts if p.strip()).strip()

    # --- день ---

    def _transcript(self, session_id: str) -> str:
        messages = get_session_messages(session_id, directory=str(self.brain_dir))
        lines: list[str] = []
        for item in messages:
            payload = item.message or {}
            role = payload.get("role") or item.type
            content = payload.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            text = text.strip()
            if text:
                who = "Василий" if role == "user" else "Коуч"
                lines.append(f"{who}: {text}")
        return "\n\n".join(lines)

    async def make_day(self, session_id: str, day: date) -> Path | None:
        transcript = self._transcript(session_id)
        if len(transcript) < 200:
            log.info("за %s говорить не о чем — выжимку не делаю", day)
            return None

        summary = await self._summarize(DAY_PROMPT + transcript)
        if not summary or summary.strip().upper().startswith("ПУСТО"):
            return None

        self.paths.days.mkdir(parents=True, exist_ok=True)
        path = self.paths.days / f"{day.isoformat()}.md"
        path.write_text(f"# {day.isoformat()} — день\n\n{summary}\n", encoding="utf-8")
        log.info("выжимка дня записана: %s", path)
        return path

    # --- укрупнение ---

    async def _rollup(self, sources: list[Path], target: Path, title: str, period: str) -> Path | None:
        if len(sources) < 2:
            return None
        body = "\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in sorted(sources))
        summary = await self._summarize(ROLLUP_PROMPT.format(period=period) + body)
        if not summary:
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# {title}\n\n{summary}\n", encoding="utf-8")
        log.info("укрупнение записано: %s", target)
        return target

    async def make_week(self, week_end: date) -> Path | None:
        """Собрать прошедшую неделю в одну запись и убрать дни за неё."""
        start = week_end - timedelta(days=6)
        days = [
            path
            for path in self.paths.days.glob("*.md")
            if start.isoformat() <= path.stem <= week_end.isoformat()
        ] if self.paths.days.exists() else []

        target = self.paths.weeks / f"{start.isoformat()}_{week_end.isoformat()}.md"
        result = await self._rollup(days, target, f"Неделя {start} — {week_end}", "неделя")
        if result:
            for path in days:
                path.unlink(missing_ok=True)
        return result

    async def make_month(self, any_day_of_month: date) -> Path | None:
        """Свернуть недели прошедшего месяца — так память старше месяца ужимается сильнее."""
        first = any_day_of_month.replace(day=1)
        prefix_start = (first - timedelta(days=1)).replace(day=1)
        weeks = [
            path
            for path in self.paths.weeks.glob("*.md")
            if path.stem[:7] == prefix_start.isoformat()[:7]
        ] if self.paths.weeks.exists() else []

        target = self.paths.months / f"{prefix_start.isoformat()[:7]}.md"
        result = await self._rollup(weeks, target, f"Месяц {prefix_start.isoformat()[:7]}", "месяц")
        if result:
            for path in weeks:
                path.unlink(missing_ok=True)
        return result
