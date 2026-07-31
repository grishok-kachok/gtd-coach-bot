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

# Окно памяти — ровно 15 кусков всегда: 7 последних дней, 5 последних полных
# недель, 3 последних полных месяца. Это решение, а не настройка (PROJECT.md,
# 31.07.2026): при постоянном количестве размер блока — умножение, известное
# заранее, и резать никогда ничего не нужно. Пересечение свежей недели
# с последними днями (0–6 дней) принято осознанно: недельная выжимка не копия
# дней, а другой уровень детализации.
#
# Недель именно ПЯТЬ, а не четыре. Месячная выжимка закрывает месяц только когда
# он кончился, а четыре недели дотягиваются назад ровно на 28 дней от последнего
# воскресенья — и если оно пришлось на 29–31 число, между концом прошлого месяца
# и началом недель остаётся провал в 1–3 дня, которых не знает никто. Найдено
# прогоном года в tests/test_rotation.py, а не рассуждением; пятая неделя
# закрывает его при любом календаре.
WINDOW = {"month": 3, "week": 5, "day": 7}

# От крупного и старого к свежему: так коуч читает историю в ту же сторону,
# в какую она случалась.
LABELS = {"month": "Месяц", "week": "Неделя", "day": "День"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    session_id  TEXT,
    role        TEXT NOT NULL,          -- vasiliy | coach
    channel     TEXT NOT NULL,          -- voice | text | morning | midday | evening | nudge
    text        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'telegram',  -- telegram | laptop (ноутбук Василия)
    uuid        TEXT                    -- сквозной id из Claude Code, защита от дублей при импорте
);
CREATE INDEX IF NOT EXISTS messages_day ON messages(day);
-- индекс по uuid создаётся в _migrate: на живой базе колонки ещё нет в этот момент

CREATE TABLE IF NOT EXISTS digests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    period      TEXT NOT NULL,          -- day | week | month
    period_key  TEXT NOT NULL,          -- 2026-07-21 | 2026-W30 | 2026-07
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
            self._migrate(db)

    def _migrate(self, db: sqlite3.Connection) -> None:
        """Дотягиваем живую базу до свежей схемы: только добавляющие правки, ничего не теряем."""
        cols = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if "source" not in cols:
            db.execute("ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram'")
        if "uuid" not in cols:
            db.execute("ALTER TABLE messages ADD COLUMN uuid TEXT")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS messages_uuid ON messages(uuid)")

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
                    "INSERT INTO messages(day, created_at, session_id, role, channel, text, source, uuid) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (now.date().isoformat(), now.isoformat(timespec="seconds"), session_id, role, channel, text,
                     "telegram", None),
                )
        except sqlite3.Error:
            log.exception("не смог записать сообщение в архив")

    def import_message(self, uuid: str, created_at_msk: datetime, role: str, text: str) -> bool:
        """Импорт реплики с ноутбука. Возвращает True, если строка новая (не дубль)."""
        with self._connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO messages(day, created_at, session_id, role, channel, text, source, uuid) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (created_at_msk.date().isoformat(), created_at_msk.isoformat(timespec="seconds"),
                 None, role, "text", text, "laptop", uuid),
            )
            return cur.rowcount > 0

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

    def messages_of_day(self, day: str) -> list[tuple[str, str, str, str]]:
        """Сырьё за день: (роль, канал, источник, текст) в хронологии.

        Порядок по времени, а не по id: телеграм пишется вживую, а реплики из
        ноутбука доезжают пачкой с лагом в пару минут — по id диалог перемешался бы.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT role, channel, source, text FROM messages WHERE day=? ORDER BY created_at, id", (day,)
            ).fetchall()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    def answered_since(self, moment: str) -> bool:
        """Отозвался ли Василий после указанного момента (ISO-время с зоной).

        Тексты крон-пингов лежат в архиве с ролью «vasiliy» — это инструкция коучу,
        а не слова Василия, поэтому служебные каналы из выборки исключаем. Реплика
        с ноутбука тоже считается ответом: если человек вживую работает в другом
        окне, дёргать его «приём-приём» в телеграме — верный способ приучить
        пролистывать наши сообщения.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM messages WHERE role='vasiliy' AND created_at>? "
                "AND channel NOT IN ('morning','midday','evening','nudge') LIMIT 1",
                (moment,),
            ).fetchone()
        return row is not None

    def day_digests(self, start: str, end: str) -> list[tuple[str, str]]:
        """Дневные выжимки за отрезок включительно — сырьё для укрупнения.

        Неделя и месяц собираются отсюда, а не из файлов и не из уровня выше.
        Из файлов нельзя: свёрнутые дни с диска убираются, а из базы не пропадают.
        Из уровня выше нельзя: месяц из недель — пересказ пересказа, он физически
        не может быть подробнее недель. Из сырья тоже нельзя: месяц разговоров —
        около 492 000 токенов, он не влезет (замер 31.07.2026).
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT period_key, text FROM digests WHERE period='day' "
                "AND period_key BETWEEN ? AND ? ORDER BY period_key",
                (start, end),
            ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def window_rows(self) -> list[tuple[str, str, str]]:
        """Куски окна памяти по порядку: (уровень, ключ, текст).

        Каждый уровень отбирается СВОИМ запросом. Одним запросом нельзя: ключи
        уровней разного формата (`2026-08`, `2026-W31`, `2026-08-03`), и при
        сортировке текстом недели вытесняли бы месяцы из общей выборки — так этот
        код и был сломан до 31.07.2026.
        """
        rows: list[tuple[str, str, str]] = []
        with self._connect() as db:
            for period, limit in WINDOW.items():
                picked = db.execute(
                    "SELECT period_key, text FROM digests WHERE period=? "
                    "ORDER BY period_key DESC LIMIT ?",
                    (period, limit),
                ).fetchall()
                rows.extend((period, key, text) for key, text in reversed(picked))
        return rows

    def recent_digests(self) -> str:
        """Память для начала нового разговора — окно WINDOW, склеенное в текст.

        Этот текст подставляется в первый запрос новой сессии, но НЕ пишется в
        архив как сообщение — иначе завтрашний транскрипт вобрал бы вчерашние
        выжимки, и они бы разрастались от пересказа к пересказу.
        """
        return "\n\n".join(
            f"{LABELS[period]} {key}:\n{text}" for period, key, text in self.window_rows()
        )
