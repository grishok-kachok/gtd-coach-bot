"""Архив разговоров: сырьё и выжимки в одной таблице-хранилище.

Выжимки в журнале — это то, что коуч читает каждый день. Архив — то, что
можно перелопатить, если выжимка что-то упустила или понадобилось поднять
старое дословно. SQLite выбран не от бедности: файл лежит в docker-томе,
а на Bronto тома со SQLite уже бэкапятся тем же скриптом, что и Postgres.
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
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    session_id  TEXT,
    role        TEXT NOT NULL,          -- vasiliy | coach
    channel     TEXT NOT NULL,          -- voice | text | morning | evening
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_day ON messages(day);

CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period      TEXT NOT NULL,          -- day | week | month
    period_key  TEXT NOT NULL,          -- 2026-07-21 | 2026-07-15_2026-07-21 | 2026-07
    created_at  TEXT NOT NULL,
    text        TEXT NOT NULL,
    UNIQUE(period, period_key)
);
"""


class Archive:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # Пишем из обработчиков сообщений, поэтому не блокируем цикл событий.
    async def add_message(self, role: str, channel: str, text: str, session_id: str | None) -> None:
        await asyncio.to_thread(self._add_message, role, channel, text, session_id)

    def _add_message(self, role: str, channel: str, text: str, session_id: str | None) -> None:
        now = datetime.now(MOSCOW)
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO messages(day, created_at, session_id, role, channel, text) "
                    "VALUES(?,?,?,?,?,?)",
                    (now.date().isoformat(), now.isoformat(timespec="seconds"), session_id, role, channel, text),
                )
        except sqlite3.Error:
            log.exception("не смог записать сообщение в архив")

    async def add_digest(self, period: str, period_key: str, text: str) -> None:
        await asyncio.to_thread(self._add_digest, period, period_key, text)

    def _add_digest(self, period: str, period_key: str, text: str) -> None:
        now = datetime.now(MOSCOW).isoformat(timespec="seconds")
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO digests(period, period_key, created_at, text) VALUES(?,?,?,?) "
                    "ON CONFLICT(period, period_key) DO UPDATE SET text=excluded.text, created_at=excluded.created_at",
                    (period, period_key, now, text),
                )
        except sqlite3.Error:
            log.exception("не смог записать выжимку в архив")

    def messages_of_day(self, day: str) -> list[tuple[str, str, str]]:
        """Сырьё за день: (роль, канал, текст) по порядку."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT role, channel, text FROM messages WHERE day=? ORDER BY id", (day,)
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]
