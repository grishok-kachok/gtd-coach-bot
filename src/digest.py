"""Ночная выжимка: разговор дня превращается в короткую запись в журнале.

Смысл — не ждать, пока движок сам сожмёт разговор при упоре в лимит (тогда
он режет вслепую и теряет мелочи), а раз в сутки перечитать день целиком самой
умной моделью и оставить плотный конспект. Из этих дневных конспектов —
и только из них — собираются недели и месяцы: каждый уровень строится из ДНЕЙ,
а не из уровня выше. Так подробность уровня остаётся решением, а не следствием.
Читает коуч ровно 15 кусков: 7 дней, 5 недель, 3 месяца (см. WINDOW в archive.py).
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

from .archive import WINDOW, Archive

log = logging.getLogger(__name__)

DAY_PROMPT = """Ниже — стенограмма разговора коуча с Василием за {day}.

Сделай плотную выжимку этого дня. Требования:
- Дата дня — {day}, и никакая другая. Слова «сегодня» и «завтра» в стенограмме
  считай от неё. Своей строки с датой и заголовка не пиши: они уже есть в заметке.
- Что Василий делал, что решил, о чём договорились, что его беспокоит.
- Обязательно сохрани конкретику: имена, суммы, даты, названия задач и проектов.
- Отдельной строкой — что изменилось в планах или сроках, если менялось.
- Отдельной строкой — новое понимание про Василия (что сработало, что бесит), если проявилось.
- Не пересказывай вежливости и служебную возню с инструментами.
- Пиши сжато, но не ценой фактов: лучше лишняя строка, чем потерянная деталь.
- Простой текст, без markdown-заголовков. 10–20 строк.

Проверь себя по пяти вопросам — по одной твоей выжимке коуч должен уметь
ответить на каждый: что Василий обещал и сдержал ли; что застряло и почему;
что нового узнали про него самого; какие решения приняты; какое у него
состояние сил. Нет ответа — строку добавь.

Если разговора по сути не было — ответь одной строкой: ПУСТО

Стенограмма:

"""

ROLLUP_PROMPT = """Ниже — дневные выжимки за {period}. Сделай из них одну обобщённую выжимку.

- {focus}
- Убери повседневный шум, оставь линии: что двигалось, что застряло, что решили.
- Сохрани конкретику по срокам, деньгам, договорённостям и результатам.
- Отдельно — устойчивые наблюдения про Василия (паттерны, а не разовые эпизоды).
- Простой текст, без markdown-заголовков. 15–30 строк.

Дневные выжимки:

"""

# У каждого уровня своя работа, иначе месяц получается пересказом недели.
# День — факты; неделя — что сдвинулось и что застряло; месяц — траектория.
LEVELS = {
    "week": ("неделю", "Работа этого уровня — что за неделю сдвинулось, а что застряло и почему."),
    "month": ("месяц", "Работа этого уровня — траектория месяца: куда всё двигалось, а не перечень дел."),
}
KIND = {"week": "недельная выжимка", "month": "месячная выжимка"}

# Выжимка — заметка мозга по стандарту Loreground, а не просто текстовый файл.
# Заголовок писался руками при перестройке памяти в этапе 12, но пишет-то файлы
# код: первая же новая выжимка вышла бы без заголовка, а валидатор считает такую
# заметку битой. Тип — `source`: выжимка ничего не утверждает про Василия, она
# фиксирует сказанное. Корень провенанса — день разговора, у укрупнений корни
# те дни, из которых их собрали: пересказ своего корня не заводит.
FRONTMATTER = """---
title: {title}
type: source
schema_version: "1.0"
status: stable
created: {created}
source_type: personal-experience
reliability: C
author: {author}
ref: {ref}
root_id: [{roots}]
tags: [выжимка]
---

"""

# Список выжимок в точке входа собирается кодом между этими метками. Руками
# его вести нельзя: файлы ротируются каждую ночь, а ссылка на удалённый файл —
# это битая ссылка, которую валидатор находит, а человек нет.
INDEX_FILE = "00-index.md"
INDEX_START = "<!-- начало:выжимки — собирается кодом, src/digest.py -->"
INDEX_END = "<!-- конец:выжимки -->"


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
    def __init__(self, brain_dir: Path, archive: Archive, model: str = "claude-fable-5") -> None:
        self.brain_dir = brain_dir
        self.archive = archive
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

    def _transcript_from_archive(self, day: date) -> str:
        lines = []
        for role, channel, source, text in self.archive.messages_of_day(day.isoformat()):
            who = "Василий" if role == "vasiliy" else "Коуч"
            mark = " (голосом)" if channel == "voice" else ""
            if source == "laptop":
                mark = " (с ноутбука)"
            lines.append(f"{who}{mark}: {text}")
        return "\n\n".join(lines)

    async def make_day(self, session_id: str | None, day: date) -> Path | None:
        # Архив — основной источник: он переживает и обнуление сессии, и пересборку контейнера.
        transcript = self._transcript_from_archive(day)
        if len(transcript) < 200 and session_id:
            transcript = self._transcript(session_id)
        if len(transcript) < 200:
            log.info("за %s говорить не о чем — выжимку не делаю", day)
            return None

        summary = await self._summarize(DAY_PROMPT.format(day=day.isoformat()) + transcript)
        if not summary or summary.strip().upper().startswith("ПУСТО"):
            return None

        self.paths.days.mkdir(parents=True, exist_ok=True)
        path = self.paths.days / f"{day.isoformat()}.md"
        head = FRONTMATTER.format(
            title=day.isoformat(),
            created=day.isoformat(),
            author="Василий (со слов) + агент-коуч (запись)",
            ref=f"разговор {day.isoformat()}, записан агентом в мозг",
            roots=f"разговор-{day.isoformat()}",
        )
        path.write_text(f"{head}# {day.isoformat()} — день\n\n{summary}\n", encoding="utf-8")
        await self.archive.add_digest("day", day.isoformat(), summary)
        log.info("выжимка дня записана: %s", path)
        return path

    # --- укрупнение ---

    async def _rollup(
        self,
        days: list[tuple[str, str]],
        target: Path,
        title: str,
        period: str,
        period_key: str,
        closed: date,
    ) -> Path | None:
        """Собрать уровень из дневных выжимок. Один день — укрупнять нечего.

        `closed` — последний день периода: из него берётся дата сборки, чтобы
        заголовок заметки не зависел от того, когда именно запустили код.
        """
        if len(days) < 2:
            log.info("%s %s: дневных выжимок %d — укрупнять нечего", period, period_key, len(days))
            return None
        body = "\n\n---\n\n".join(f"{key}:\n{text}" for key, text in days)
        period_name, focus = LEVELS[period]
        summary = await self._summarize(ROLLUP_PROMPT.format(period=period_name, focus=focus) + body)
        if not summary:
            return None
        head = FRONTMATTER.format(
            title=period_key,
            created=(closed + timedelta(days=1)).isoformat(),
            author="агент-коуч (сборка из дневных выжимок)",
            ref=f"{KIND[period]} {period_key}, собрана из дней {days[0][0]} — {days[-1][0]}",
            roots=", ".join(f"разговор-{key}" for key, _ in days),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{head}# {title}\n\n{summary}\n", encoding="utf-8")
        await self.archive.add_digest(period, period_key, summary)
        log.info("укрупнение записано: %s (дней на входе: %d)", target, len(days))
        return target

    async def make_week(self, week_end: date) -> Path | None:
        """Свернуть календарную неделю Пн–Вс, которая закончилась `week_end`.

        Ключ — номер ISO (`2026-W30`), а не диапазон дат: диапазон сортировался
        текстом вперемешку с ключами месяцев и вытеснял их из окна памяти.
        """
        start = week_end - timedelta(days=6)
        iso = week_end.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        days = self.archive.day_digests(start.isoformat(), week_end.isoformat())
        target = self.paths.weeks / f"{key}.md"
        title = f"Неделя {key} ({start.isoformat()} — {week_end.isoformat()})"
        return await self._rollup(days, target, title, "week", key, closed=week_end)

    async def make_month(self, day_of_month: date) -> Path | None:
        """Свернуть календарный месяц, которому принадлежит `day_of_month`.

        Именно календарный: раньше месяц собирался из недель, которые в нём
        *начались*, — и неделя 29 июня – 5 июля числилась июньской, а первых
        пяти дней июля в июльском месяце не было вовсе.
        """
        first = day_of_month.replace(day=1)
        last = (first + timedelta(days=31)).replace(day=1) - timedelta(days=1)
        key = first.isoformat()[:7]
        days = self.archive.day_digests(first.isoformat(), last.isoformat())
        target = self.paths.months / f"{key}.md"
        return await self._rollup(days, target, f"Месяц {key}", "month", key, closed=last)

    # --- ротация журнала ---

    def rotate(self) -> None:
        """Привести журнал в мозге к тому же окну, что читает коуч, и починить адреса.

        Два действия неразделимы: удалить файл, оставив ссылку на него в точке
        входа, — значит завести битую ссылку. Поэтому одна дверь, а не две.
        """
        self._prune()
        self._refresh_index()

    def _prune(self) -> None:
        """Держать в журнале то же окно, что видит коуч: 7 дневных файлов и 5 недельных.

        Удаляются только файлы. Тексты остаются строками в базе (оттуда их берут
        и окно памяти, и укрупнение) и в истории git — потери нет. Месяцы не
        трогаем: их дюжина в год, это долгая память, которую человек листает
        руками.
        """
        for folder, keep in ((self.paths.days, WINDOW["day"]), (self.paths.weeks, WINDOW["week"])):
            if not folder.exists():
                continue
            for path in sorted(folder.glob("*.md"))[:-keep]:
                path.unlink(missing_ok=True)
                log.info("файл выжимки убран из журнала: %s", path.name)

    def _refresh_index(self) -> None:
        """Переписать список выжимок в точке входа памяти между метками.

        Меток нет — молча ничего не делаем и говорим об этом в лог: чужой файл
        код правит только там, где ему это разрешили явно.
        """
        index = self.brain_dir / "память" / INDEX_FILE
        if not index.exists():
            return
        text = index.read_text(encoding="utf-8")
        if INDEX_START not in text or INDEX_END not in text:
            log.warning("в %s нет меток списка выжимок — список не обновлён", index)
            return

        lines = []
        for folder, name in ((self.paths.months, "месяцы"), (self.paths.weeks, "недели"),
                             (self.paths.days, "дни")):
            keys = sorted(path.stem for path in folder.glob("*.md")) if folder.exists() else []
            if keys:
                lines.append(" · ".join(f"[[{key}]]" for key in keys) + f" — {name}")
        block = "\n".join(f"- {line}" for line in lines) or "- пока пусто"

        head, _, rest = text.partition(INDEX_START)
        _, _, tail = rest.partition(INDEX_END)
        index.write_text(f"{head}{INDEX_START}\n{block}\n{INDEX_END}{tail}", encoding="utf-8")
        log.info("список выжимок в точке входа обновлён: %d строк", len(lines))
