"""Профиль: показатели считаются правдой и раскладываются по закону модуля.

Главное, что здесь проверяется, — не формулы, а три вещи, на которых профиль
легко соврал бы: время приводится к местному (Todoist пишет в UTC), медиана
не утаскивается одной старой задачей, и запись идёт в тот дом, где положено —
цифры в состояние, ряд в журнал, ссылки в точку входа.
"""

import sqlite3
from datetime import date

import pytest

from src import profile
from src.todoist_snapshot import SCHEMA


@pytest.fixture
def база(tmp_path):
    путь = tmp_path / "coach.db"
    with sqlite3.connect(путь) as db:
        db.executescript(SCHEMA)
    return путь


def закрыть(база, task_id, completed_at, added_at="", labels="", project="Рабочее",
            content="дело"):
    with sqlite3.connect(база) as db:
        db.execute(
            "INSERT OR REPLACE INTO todoist_closed(task_id, completed_at, content,"
            " project, added_at, labels, priority, due) VALUES(?,?,?,?,?,?,?,?)",
            (task_id, completed_at, content, project, added_at, labels, 1, ""),
        )


def открыть(соединение_путь, day, task_id, project="Рабочее"):
    with sqlite3.connect(соединение_путь) as db:
        db.execute(
            "INSERT OR REPLACE INTO todoist_tasks(day, task_id, content, description,"
            " project, section, parent_id, labels, priority, due, deadline, duration,"
            " added_at, comments) VALUES(?,?,?,'',?,'',NULL,'',1,'','',NULL,'','[]')",
            (day, task_id, "лежит", project),
        )


# ── часы работы ──────────────────────────────────────────────────────────────

def test_часы_приводятся_к_местному_времени(база):
    """Todoist пишет UTC. 09:06Z — это московские 12:06, и путать нельзя:
    иначе показатель посоветует двигать чек-ин не туда."""
    закрыть(база, "t1", "2026-08-01T09:06:38.010100Z")
    with sqlite3.connect(база) as db:
        показатель = profile.часы_работы(db, 30, date(2026, 8, 2))
    assert показатель and "12:00 — 1" in показатель.значение


def test_доля_до_полудня_считается(база):
    закрыть(база, "t1", "2026-08-01T05:00:00Z")   # 08:00 МСК
    закрыть(база, "t2", "2026-08-01T15:00:00Z")   # 18:00 МСК
    with sqlite3.connect(база) as db:
        показатель = profile.часы_работы(db, 30, date(2026, 8, 2))
    assert "до полудня 50 %" in показатель.значение


def test_старое_за_окном_не_считается(база):
    закрыть(база, "t1", "2026-01-01T09:00:00Z")
    with sqlite3.connect(база) as db:
        assert profile.часы_работы(db, 30, date(2026, 8, 2)) is None


# ── срок жизни ───────────────────────────────────────────────────────────────

def test_срок_жизни_медианой_а_не_средним(база):
    """Одна задача, пролежавшая полгода, не должна врать про обычный день."""
    закрыть(база, "t1", "2026-08-01T09:00:00Z", added_at="2026-07-31T09:00:00Z")
    закрыть(база, "t2", "2026-08-01T09:00:00Z", added_at="2026-07-30T09:00:00Z")
    закрыть(база, "t3", "2026-08-01T09:00:00Z", added_at="2026-02-01T09:00:00Z")
    with sqlite3.connect(база) as db:
        показатель = profile.срок_жизни(db, 90, date(2026, 8, 2))
    assert "медиана 2.0 дн." in показатель.значение


def test_срок_жизни_разделяет_по_размеру(база):
    закрыть(база, "t1", "2026-08-01T09:00:00Z", added_at="2026-08-01T08:00:00Z",
            labels="актив,⏱️S")
    закрыть(база, "t2", "2026-08-01T09:00:00Z", added_at="2026-07-22T09:00:00Z",
            labels="⏱️L")
    with sqlite3.connect(база) as db:
        показатель = profile.срок_жизни(db, 90, date(2026, 8, 2))
    assert "⏱️S: 0.0 дн." in показатель.значение and "⏱️L: 10.0 дн." in показатель.значение


def test_закрытая_без_времени_заведения_не_ломает_счёт(база):
    закрыть(база, "t1", "2026-08-01T09:00:00Z", added_at="")
    with sqlite3.connect(база) as db:
        assert profile.срок_жизни(db, 90, date(2026, 8, 2)) is None


# ── где идёт работа ──────────────────────────────────────────────────────────

def test_видно_и_закрытое_и_лежащее(база):
    закрыть(база, "t1", "2026-08-01T09:00:00Z", project="Рабочее")
    открыть(база, "2026-08-02", "t9", project="Личное")
    with sqlite3.connect(база) as db:
        показатель = profile.что_делается(db, 30, date(2026, 8, 2))
    assert "Рабочее: закрыто 1, лежит 0" in показатель.значение
    assert "Личное: закрыто 0, лежит 1" in показатель.значение


# ── обещание дня ─────────────────────────────────────────────────────────────

def _обещание(база, day, обещание, исход, главное="да"):
    with sqlite3.connect(база) as db:
        db.execute("CREATE TABLE IF NOT EXISTS day_promise (day TEXT PRIMARY KEY,"
                   " обещание TEXT, исход TEXT, главное TEXT)")
        db.execute("INSERT OR REPLACE INTO day_promise VALUES(?,?,?,?)",
                   (day, обещание, исход, главное))


def test_обещания_считают_попадания(база):
    _обещание(база, "2026-08-01", "дожать лендинг", "выполнено")
    _обещание(база, "2026-07-31", "смонтировать урок", "нет", главное="другое")
    with sqlite3.connect(база) as db:
        показатель = profile.обещания(db, 30, date(2026, 8, 2))
    assert "сдержано 1 из 2" in показатель.значение
    assert "в 1 случаях закрыл другое" in показатель.значение


def test_день_без_обещания_в_счёт_не_идёт(база):
    _обещание(база, "2026-08-01", "", "неизвестно", главное="нет данных")
    with sqlite3.connect(база) as db:
        assert profile.обещания(db, 30, date(2026, 8, 2)) is None


def test_нет_таблицы_нет_падения(база):
    with sqlite3.connect(база) as db:
        assert profile.обещания(db, 30, date(2026, 8, 2)) is None


# ── отклик на пинги ──────────────────────────────────────────────────────────

def _архив(tmp_path, строки):
    путь = tmp_path / "архив.db"
    with sqlite3.connect(путь) as db:
        db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                   " day TEXT, created_at TEXT, session_id TEXT, role TEXT,"
                   " channel TEXT, text TEXT)")
        for created_at, role, channel in строки:
            db.execute("INSERT INTO messages(day, created_at, role, channel, text)"
                       " VALUES(?,?,?,?,'')",
                       (created_at[:10], created_at, role, channel))
    return путь


def test_отклик_меряет_задержку_ответа(tmp_path):
    путь = _архив(tmp_path, [
        ("2026-08-01T10:00:00+03:00", "vasiliy", "morning"),
        ("2026-08-01T10:20:00+03:00", "vasiliy", "voice"),
    ])
    показатель = profile.отклик_на_пинги(путь, 30, date(2026, 8, 2))
    assert "morning: 1 из 1, обычно через 20 мин" in показатель.значение


def test_пинг_без_ответа_считается_пропущенным(tmp_path):
    путь = _архив(tmp_path, [("2026-08-01T20:00:00+03:00", "vasiliy", "evening")])
    показатель = profile.отклик_на_пинги(путь, 30, date(2026, 8, 2))
    assert "evening: 0 из 1, обычно через без ответа" in показатель.значение
    assert показатель.основание == 1


def test_ответ_коуча_за_ответ_человека_не_считается(tmp_path):
    путь = _архив(tmp_path, [
        ("2026-08-01T10:00:00+03:00", "vasiliy", "morning"),
        ("2026-08-01T10:01:00+03:00", "coach", "text"),
    ])
    показатель = profile.отклик_на_пинги(путь, 30, date(2026, 8, 2))
    assert "0 из 1" in показатель.значение


# ── запись по закону модуля ──────────────────────────────────────────────────

@pytest.fixture
def мозг(tmp_path):
    корень = tmp_path / "brain"
    (корень / "память" / "состояние").mkdir(parents=True)
    (корень / "память" / "журнал").mkdir(parents=True)
    (корень / "память" / "00-index.md").write_text("# Память\n\n## Что где\n", encoding="utf-8")
    return корень


def _показатель(значение="раз"):
    return profile.Показатель("Часы закрытий", значение, "следствие", 30, 12)


def test_состояние_перезаписывается_а_не_копится(мозг):
    profile.записать(мозг, [_показатель("первое")], date(2026, 8, 1))
    profile.записать(мозг, [_показатель("второе")], date(2026, 8, 2))
    текст = (мозг / "память" / "состояние" / profile.СОСТОЯНИЕ).read_text(encoding="utf-8")
    assert "второе" in текст and "первое" not in текст
    assert "as_of: 2026-08-02" in текст
    assert "created: 2026-08-01" in текст, "дата заведения заметки не должна съезжать"


def test_журнал_копится_и_только_по_просьбе(мозг):
    profile.записать(мозг, [_показатель("будни")], date(2026, 8, 1))
    замеры = мозг / "память" / "журнал" / profile.ЗАМЕРЫ
    assert not замеры.exists(), "в журнал пишем раз в неделю, а не каждую ночь"

    profile.записать(мозг, [_показатель("неделя 1")], date(2026, 8, 2), в_журнал=True)
    profile.записать(мозг, [_показатель("неделя 2")], date(2026, 8, 9), в_журнал=True)
    текст = замеры.read_text(encoding="utf-8")
    assert "неделя 1" in текст and "неделя 2" in текст
    assert текст.count("## ") == 2


def test_ссылки_появляются_в_точке_входа(мозг):
    profile.записать(мозг, [_показатель()], date(2026, 8, 1))
    индекс = (мозг / "память" / "00-index.md").read_text(encoding="utf-8")
    assert "[[профиль-показатели]]" in индекс and "[[профиль-замеры]]" in индекс


def test_ссылки_не_плодятся_при_каждом_прогоне(мозг):
    for день in range(1, 5):
        profile.записать(мозг, [_показатель()], date(2026, 8, день))
    индекс = (мозг / "память" / "00-index.md").read_text(encoding="utf-8")
    assert индекс.count("[[профиль-показатели]]") == 1


def test_нет_показателей_нет_файлов(мозг):
    assert profile.записать(мозг, [], date(2026, 8, 1)) == []
    assert not (мозг / "память" / "состояние" / profile.СОСТОЯНИЕ).exists()


def test_показатель_всегда_несёт_следствие():
    """Показатель без следствия — украшение. Форма это и держит."""
    строка = _показатель().строка()
    assert "следствие" in строка and "12 событий за 30 дн." in строка


# ── переносы (счёт самого Todoist) ───────────────────────────────────────────

def test_переносы_считаются_медианой(база):
    for i, переносов in enumerate((0, 0, 4)):
        with sqlite3.connect(база) as db:
            db.execute(
                "INSERT INTO todoist_closed(task_id, completed_at, content, project,"
                " added_at, labels, priority, due, postponed) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"t{i}", "2026-08-01T09:00:00Z", "дело", "Рабочее", "", "", 1, "", переносов),
            )
    with sqlite3.connect(база) as db:
        показатель = profile.переносы(db, 90, date(2026, 8, 2))
    assert "медиана 0" in показатель.значение
    assert "с первого раза 67 %" in показатель.значение
    assert "переехавших трижды и больше: 1" in показатель.значение


def test_где_идёт_работа_молчит_когда_проекты_не_спрашивали(база):
    """Ноль читается как факт «там не работали». Куплено прогоном 01.08:
    добор истории не спрашивал имена проектов, и показатель объявил закрытыми
    ноль задач по всем проектам — при 56 закрытых в базе."""
    закрыть(база, "t1", "2026-08-01T09:00:00Z", project="")
    открыть(база, "2026-08-02", "t9", project="Личное")
    with sqlite3.connect(база) as db:
        assert profile.что_делается(db, 30, date(2026, 8, 2)) is None
