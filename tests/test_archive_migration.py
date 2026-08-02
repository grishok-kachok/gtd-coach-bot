"""Переименование роли человека — операция с данными, а не правка текста.

До 02.08.2026 роль человека звалась именем владельца. В живом архиве под этим
именем лежат сотни строк, а запросы кода ищут по точному значению. Значит
проверять надо не то, что в исходниках нет старого слова, а то, что старая
база после запуска бота **не теряет ни одной строки** и продолжает отвечать
на те же вопросы.
"""

from __future__ import annotations

import sqlite3

from src.archive import Archive

СТАРАЯ_СХЕМА = """
CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    session_id  TEXT,
    role        TEXT NOT NULL,
    channel     TEXT NOT NULL,
    text        TEXT NOT NULL
);
"""

СТРОКИ = [
    ("2026-07-30", "2026-07-30T10:00:00+03:00", "vasiliy", "morning", "утренний пинг"),
    ("2026-07-30", "2026-07-30T10:05:00+03:00", "vasiliy", "text", "доброе утро"),
    ("2026-07-30", "2026-07-30T10:06:00+03:00", "coach", "text", "и тебе"),
    ("2026-07-31", "2026-07-31T09:00:00+03:00", "vasiliy", "voice", "наговорил дело"),
]


def _старая_база(tmp_path):
    путь = tmp_path / "coach.db"
    db = sqlite3.connect(путь)
    db.executescript(СТАРАЯ_СХЕМА)
    db.executemany(
        "INSERT INTO messages(day, created_at, role, channel, text) VALUES(?,?,?,?,?)",
        СТРОКИ,
    )
    db.commit()
    db.close()
    return путь


def test_старая_роль_переезжает_без_потерь(tmp_path):
    путь = _старая_база(tmp_path)
    было = sqlite3.connect(путь).execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    Archive(путь)  # открытие архива и есть миграция

    db = sqlite3.connect(путь)
    стало = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    роли = dict(db.execute("SELECT role, COUNT(*) FROM messages GROUP BY role").fetchall())

    assert стало == было == len(СТРОКИ), "миграция потеряла или размножила строки"
    assert роли == {"user": 3, "coach": 1}
    assert "vasiliy" not in роли


def test_миграция_идемпотентна(tmp_path):
    путь = _старая_база(tmp_path)
    Archive(путь)
    Archive(путь)  # второй запуск бота на той же базе
    db = sqlite3.connect(путь)
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == len(СТРОКИ)
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role='user'").fetchone()[0] == 3


def test_ответ_человека_находится_после_переименования(tmp_path):
    """Главная проверка: запрос кода ищет по точному значению роли.

    Пропусти строку в UPDATE — и `answered_since` тихо начнёт отвечать «молчит»
    на все прошлые реплики. Молча, без единой ошибки в логах.
    """
    путь = _старая_база(tmp_path)
    архив = Archive(путь)
    assert архив.answered_since("2026-07-30T10:00:00+03:00") is True
    assert архив.answered_since("2026-07-31T23:00:00+03:00") is False
