"""Память канала комментариев: закладка опроса, свои комментарии, журнал действий.

Три таблицы, и каждая заведена под свою беду.

**Закладка опроса** (`comment_state`). Todoist умеет отдавать только новое —
по `sync_token` из прошлого ответа. Токен обязан пережить перезапуск бота,
иначе после каждого рестарта канал либо забывал бы пропущенное, либо
перечитывал всю историю заново.

**Реестр своих комментариев** (`comment_mine`). Бот пишет комментарии токеном
Василия, и в следующем опросе его собственный ответ приходит с тем же
`posted_uid`, что и хозяйский, — проверено прогоном 01.08.2026. По автору
своё от чужого не отличается **в принципе**, поэтому реестр не удобство,
а единственный надёжный признак. Он в базе, а не в памяти процесса: рестарт
посреди цикла превратил бы свой ответ в чужое обращение.

**Журнал действий** (`comment_actions`). Всё, что коуч изменил по обращению,
записывается вместе с тем, **как было до**. Отсюда работает откат, а заодно
видно, что бот трогал в делах: журнал активности Todoist на бесплатном тарифе
живёт около недели, а этот — столько же, сколько архив разговоров.

Единица отката — **обращение**, а не поле: одна фраза Василия порождает
несколько изменений, и возвращать их порознь было бы издевательством.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

MOSCOW = ZoneInfo("Europe/Moscow")

# Подпись бота под своими комментариями — вторая защита от петли, независимая
# от реестра. Живёт здесь, а не в comments.py, чтобы её мог поставить и тот,
# кто пишет комментарии мимо канала (backstage.py), не таща за собой движок.
МАРКЕР = "🤖"

SCHEMA = """
CREATE TABLE IF NOT EXISTS comment_state (
    key         TEXT PRIMARY KEY,       -- sync_token
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comment_mine (
    comment_id  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS comment_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    request_id  TEXT NOT NULL,          -- id комментария-обращения: единица отката
    task_id     TEXT NOT NULL,          -- задача, которой касалось действие
    kind        TEXT NOT NULL,          -- создана | изменено | комментарий
    field       TEXT NOT NULL DEFAULT '',
    before      TEXT,                   -- как было (JSON); пусто у созданного
    after       TEXT,                   -- как стало (JSON)
    undone_at   TEXT                    -- когда откатили; пусто — действие живо
);
CREATE INDEX IF NOT EXISTS comment_actions_request ON comment_actions(request_id);
"""


@dataclass(frozen=True)
class Действие:
    """Одна запись журнала — то, что можно вернуть обратно."""

    id: int
    request_id: str
    task_id: str
    kind: str            # создана | изменено | комментарий
    field: str
    before: object
    after: object


def _db_path(path: Path | str | None = None) -> Path:
    """База та же, что у архива разговоров и снимков: один том, один бэкап."""
    return Path(path or os.environ.get("ARCHIVE_DB", "/archive/coach.db"))


class Журнал:
    """Одна дверь ко всем трём таблицам канала."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = _db_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # --- закладка опроса ---

    def токен(self) -> str | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM comment_state WHERE key='sync_token'"
            ).fetchone()
        return row[0] if row else None

    def запомнить_токен(self, token: str) -> None:
        now = datetime.now(MOSCOW).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute(
                "INSERT INTO comment_state(key, value, updated_at) VALUES('sync_token',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (token, now),
            )

    # --- реестр своих комментариев ---

    def запомнить_свой(self, comment_id: str) -> None:
        """Свой комментарий — до того, как его увидит опрос."""
        if not comment_id:
            return
        now = datetime.now(MOSCOW).isoformat(timespec="seconds")
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO comment_mine(comment_id, created_at) VALUES(?,?)",
                    (str(comment_id), now),
                )
        except sqlite3.Error:
            # Не записали — сработает вторая защита, маркер в тексте. Ронять
            # из-за этого отправку ответа нельзя: человек ждёт ответа.
            log.exception("не смог запомнить свой комментарий %s", comment_id)

    def свой(self, comment_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM comment_mine WHERE comment_id=?", (str(comment_id),)
            ).fetchone()
        return row is not None

    # --- журнал действий ---

    def записать(self, request_id: str, task_id: str, kind: str,
                 field: str = "", before: object = None, after: object = None) -> None:
        now = datetime.now(MOSCOW).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute(
                "INSERT INTO comment_actions(created_at, request_id, task_id, kind, field, before, after) "
                "VALUES(?,?,?,?,?,?,?)",
                (now, str(request_id), str(task_id), kind, field,
                 json.dumps(before, ensure_ascii=False) if before is not None else None,
                 json.dumps(after, ensure_ascii=False) if after is not None else None),
            )

    def отметить_обращение(self, request_id: str, task_id: str, текст: str) -> None:
        """Отметка «обращение разобрано» — даже если оно ничего не изменило.

        Нужна ограничителю: считать по изменениям нельзя, иначе двадцать
        уточняющих вопросов подряд не заметит никто. Откату эта строка
        не мешает — он берёт только настоящие действия.
        """
        self.записать(request_id, task_id, "обращение", after=текст[:200])

    def последнее_обращение(self) -> str | None:
        """Обращение, чьи действия ещё не откачены. Единица отката — оно.

        Обращения без изменений пропускаются: «откати последнее» должно
        вернуть последнюю правку, а не упереться в уточняющий вопрос.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT request_id FROM comment_actions WHERE undone_at IS NULL "
                "AND kind<>'обращение' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None

    def было_ли_что_откатывать(self) -> bool:
        """Были ли вообще действия по комментариям — хоть когда-нибудь.

        Нужна ровно для одной фразы: «я ничего не менял» и «всё уже откачено» —
        разные ответы, и путать их значит врать человеку про свою же работу.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM comment_actions WHERE kind<>'обращение' LIMIT 1"
            ).fetchone()
        return row is not None

    def действия(self, request_id: str) -> list[Действие]:
        """Живые действия обращения, свежие первыми — откатывать надо с конца."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, request_id, task_id, kind, field, before, after "
                "FROM comment_actions WHERE request_id=? AND undone_at IS NULL "
                "AND kind<>'обращение' ORDER BY id DESC",
                (str(request_id),),
            ).fetchall()
        return [
            Действие(
                id=row[0], request_id=row[1], task_id=row[2], kind=row[3], field=row[4],
                before=json.loads(row[5]) if row[5] is not None else None,
                after=json.loads(row[6]) if row[6] is not None else None,
            )
            for row in rows
        ]

    def пометить_откат(self, ids: list[int]) -> None:
        if not ids:
            return
        now = datetime.now(MOSCOW).isoformat(timespec="seconds")
        with self._connect() as db:
            db.executemany(
                "UPDATE comment_actions SET undone_at=? WHERE id=?",
                [(now, i) for i in ids],
            )

    def обращений_за_час(self, момент: datetime) -> int:
        """Сколько обращений разобрано за последний час — для ограничителя."""
        рубеж = datetime.fromtimestamp(момент.timestamp() - 3600, MOSCOW)
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM comment_actions WHERE kind='обращение' AND created_at > ?",
                (рубеж.isoformat(timespec="seconds"),),
            ).fetchone()
        return int(row[0] or 0)


def запомнить_свой(comment_id: str, path: Path | str | None = None) -> None:
    """Короткий вход для тех, у кого нет своего журнала под рукой (backstage).

    Не бросает **ничего**. Реестр — одна из двух защит от петли, вторая
    (маркер) уже в тексте комментария; ронять из-за него отправку нельзя.
    Куплено своим же тестом: недоступная база бросала OSError на mkdir,
    вызывающий ловил его как «сеть недоступна» и рапортовал, что комментарий
    не доехал, — хотя тот уже был отправлен.
    """
    try:
        Журнал(path).запомнить_свой(comment_id)
    except Exception:
        log.exception("реестр своих комментариев недоступен")
