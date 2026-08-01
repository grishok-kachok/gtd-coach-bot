"""Счёт контекста в токенах — факт от модели, а не пересчёт байтов.

Байт — верная единица для кода: файл столько занимает на диске. Но платит
человек **токенами**, а курс у русского markdown свой (≈4,1 Б на токен, замер
01.08.2026) и на глаз не считается. Отсюда правило: цена берётся из `usage`
ответа модели — это её собственный счёт, а не наша прикидка.

Что кладём в базу после каждого ответа:

    at            когда, ISO с зоной
    day           день по Москве — по нему группируем сутки
    channel       telegram | morning | nightly-digest | comments | …
    mode          режим контекста, в котором собрана сессия
    model         чем думали
    input         свежие токены запроса
    cache_create  сколько положено в кэш (платится как запись)
    cache_read    сколько прочитано из кэша (дёшево, но это тоже контекст)
    output        сколько модель сказала

**Контекст = input + cache_create + cache_read.** Кэш делит цену, но не объём:
в голову модели всё это заезжает целиком. Считать «контекстом» один лишь
`input` значило бы объявить, что после первого хода сессия ничего не весит.

Таблица живёт в том же архиве, что разговоры и выжимки: том уже в штатном
бэкапе, и второй базы заводить незачем.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MOSCOW = ZoneInfo("Europe/Moscow")

SCHEMA = """
CREATE TABLE IF NOT EXISTS context_cost (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    day           TEXT NOT NULL,
    channel       TEXT NOT NULL,
    mode          TEXT NOT NULL,
    model         TEXT NOT NULL DEFAULT '',
    input         INTEGER NOT NULL DEFAULT 0,
    cache_create  INTEGER NOT NULL DEFAULT 0,
    cache_read    INTEGER NOT NULL DEFAULT 0,
    output        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS context_cost_day ON context_cost(day);
"""


def разобрать(usage: object) -> dict[str, int]:
    """Достать четыре числа из `usage` ответа модели.

    Форма поля — договор чужого SDK, а не наш: имена ключей уже менялись между
    версиями. Поэтому берём мягко, недостающее считаем нулём и не падаем —
    счётчик не та вещь, ради которой коуч должен онеметь.
    """
    if not isinstance(usage, dict):
        return {}
    def взять(*имена: str) -> int:
        for имя in имена:
            значение = usage.get(имя)
            if isinstance(значение, (int, float)):
                return int(значение)
        return 0

    return {
        "input": взять("input_tokens", "inputTokens"),
        "cache_create": взять("cache_creation_input_tokens", "cacheCreationInputTokens"),
        "cache_read": взять("cache_read_input_tokens", "cacheReadInputTokens"),
        "output": взять("output_tokens", "outputTokens"),
    }


def контекст(числа: dict[str, int]) -> int:
    """Сколько токенов уехало в голову модели за этот ход."""
    return числа.get("input", 0) + числа.get("cache_create", 0) + числа.get("cache_read", 0)


class ContextCost:
    """Журнал цены контекста. Пишет код, читают человек и коуч."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    async def записать(self, channel: str, mode: str, model: str, usage: object) -> dict[str, int]:
        """Отметить ход. Возвращает разобранные числа (пусто — счёта не было)."""
        числа = разобрать(usage)
        if not числа:
            return {}
        await asyncio.to_thread(self._записать, channel, mode, model, числа)
        return числа

    def _записать(self, channel: str, mode: str, model: str, числа: dict[str, int]) -> None:
        now = datetime.now(MOSCOW)
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO context_cost(at, day, channel, mode, model, input, "
                    "cache_create, cache_read, output) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now.isoformat(timespec="seconds"), now.date().isoformat(), channel,
                     mode, model, числа["input"], числа["cache_create"],
                     числа["cache_read"], числа["output"]),
                )
        except sqlite3.Error:
            log.exception("не смог записать цену контекста")

    # --- чтение ---

    def первый_ход(self, mode: str, since: str = "") -> int | None:
        """Самый дешёвый честный замер режима: контекст ПЕРВОГО хода сессии.

        Дальше в разговоре контекст растёт от самих реплик, и мерить режим
        по середине разговора бессмысленно — померишь болтовню, а не рюкзак.
        Первый ход отличаем по тому, что кэша он ещё не читал.
        """
        запрос = ("SELECT input + cache_create + cache_read FROM context_cost "
                  "WHERE mode=? AND cache_read=0")
        параметры: list[object] = [mode]
        if since:
            запрос += " AND at>=?"
            параметры.append(since)
        запрос += " ORDER BY at DESC LIMIT 1"
        with self._connect() as db:
            строка = db.execute(запрос, параметры).fetchone()
        return int(строка[0]) if строка else None

    def сутки(self, day: str) -> list[tuple[str, str, int, int, int]]:
        """Что за день: (канал, режим, ходов, контекст суммой, выдано)."""
        with self._connect() as db:
            строки = db.execute(
                "SELECT channel, mode, COUNT(*), SUM(input + cache_create + cache_read), "
                "SUM(output) FROM context_cost WHERE day=? GROUP BY channel, mode "
                "ORDER BY SUM(input + cache_create + cache_read) DESC",
                (day,),
            ).fetchall()
        return [(s[0], s[1], int(s[2]), int(s[3] or 0), int(s[4] or 0)) for s in строки]
