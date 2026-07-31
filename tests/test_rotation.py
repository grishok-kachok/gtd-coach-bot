"""Ротация памяти на синтетических датах.

Живой прогон стоит суток ожидания на каждую проверку, поэтому год жизни бота
проигрывается здесь за секунду: каждый день — дневная выжимка, по воскресеньям —
неделя, первого числа — прошлый месяц. Модель не зовём: укрупнение подменяется
заглушкой, которая запоминает, что ей дали на вход. Проверяем не текст выжимки,
а арифметику окна и границы периодов — ровно то, что было сломано до 31.07.2026.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.archive import WINDOW, Archive
from src.digest import INDEX_END, INDEX_START, Digester


class Lab:
    """Мозг и архив во временной папке плюс дневник того, что уходило в модель."""

    def __init__(self, tmp_path):
        self.archive = Archive(tmp_path / "coach.db")
        self.brain = tmp_path / "brain"
        self.digester = Digester(self.brain, self.archive)
        self.prompts: list[str] = []

        async def fake_summarize(prompt: str) -> str:
            self.prompts.append(prompt)
            return "укрупнённая выжимка"

        self.digester._summarize = fake_summarize

    def live_a_day(self, day: date) -> None:
        """То же, что делает `make_day`, но без модели: строка в базе и файл в журнале."""
        text = f"день {day.isoformat()}: что делали, о чём договорились"
        self.archive._add_digest("day", day.isoformat(), text)
        self.digester.paths.days.mkdir(parents=True, exist_ok=True)
        (self.digester.paths.days / f"{day.isoformat()}.md").write_text(
            f"# {day.isoformat()} — день\n\n{text}\n", encoding="utf-8"
        )

    def make_index(self) -> Path:
        """Точка входа памяти с метками, между которыми код ведёт список выжимок."""
        index = self.brain / "память" / "00-index.md"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            f"# Точка входа\n\n## Источники: выжимки\n\n"
            f"{INDEX_START}\n- пока пусто\n{INDEX_END}\n\n## Дальше\n",
            encoding="utf-8",
        )
        return index

    def night(self, day: date) -> None:
        """Ночной прогон в ночь после `day` — тот же порядок, что в main.nightly_digest."""
        self.live_a_day(day)
        if day.weekday() == 6:
            asyncio.run(self.digester.make_week(day))
        if (day + timedelta(days=1)).day == 1:
            asyncio.run(self.digester.make_month(day))
        self.digester.rotate()


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


# --- покрытие окна ---


def days_of(period: str, key: str) -> set[date]:
    """Какие календарные дни описывает кусок окна."""
    if period == "day":
        return {date.fromisoformat(key)}
    if period == "week":
        year, week = key.split("-W")
        start = date.fromisocalendar(int(year), int(week), 1)
        return {start + timedelta(days=i) for i in range(7)}
    first = date.fromisoformat(f"{key}-01")
    last = (first + timedelta(days=31)).replace(day=1) - timedelta(days=1)
    return {first + timedelta(days=i) for i in range((last - first).days + 1)}


def coverage(rows) -> set[date]:
    return {d for period, key, _ in rows for d in days_of(period, key)}


def holes(rows, today: date) -> list[date]:
    """Дни между самым старым покрытым и сегодняшним, которых не знает никто."""
    covered = coverage(rows)
    oldest = min(covered)
    return [
        oldest + timedelta(days=i)
        for i in range((today - oldest).days + 1)
        if oldest + timedelta(days=i) not in covered
    ]


# --- само окно ---


def test_window_is_exactly_15_pieces(lab):
    """Дефект 1: раньше бралось «все дни за 30 суток» и размер блока плясал."""
    for i in range(90):
        lab.archive._add_digest("day", (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "д")
    for i in range(1, 13):
        lab.archive._add_digest("week", f"2026-W{i:02d}", "н")
    for i in range(1, 7):
        lab.archive._add_digest("month", f"2026-{i:02d}", "м")

    rows = lab.archive.window_rows()
    assert len(rows) == sum(WINDOW.values()) == 15
    assert [period for period, _, _ in rows] == ["month"] * 3 + ["week"] * 5 + ["day"] * 7
    # От старого к свежему внутри уровня — коуч читает историю по ходу времени.
    assert [key for period, key, _ in rows if period == "month"] == ["2026-04", "2026-05", "2026-06"]
    assert [key for period, key, _ in rows if period == "week"][0] == "2026-W08"
    assert [key for period, key, _ in rows if period == "day"][-1] == "2026-03-31"


def test_weeks_do_not_squeeze_out_months(lab):
    """Дефект 2: один LIMIT 8 по разнородным ключам — и месяцы исчезали навсегда.

    `'2026-W30' > '2026-07'` при сортировке текстом, поэтому недели всегда
    оказывались «свежее» месяцев того же года.
    """
    for i in range(1, 31):
        lab.archive._add_digest("week", f"2026-W{i:02d}", "н")
    for i in range(1, 8):
        lab.archive._add_digest("month", f"2026-{i:02d}", "м")

    periods = [period for period, _, _ in lab.archive.window_rows()]
    assert periods.count("month") == 3, "месяцы вытеснены неделями — старый дефект вернулся"
    assert periods.count("week") == 5


# --- границы периодов ---


def test_week_key_is_iso_and_source_is_the_database(lab):
    """Дефект 3: укрупнение перебирало файлы, а файлы свёрнутых дней удаляются."""
    for i in range(7):
        lab.archive._add_digest("day", (date(2026, 7, 20) + timedelta(days=i)).isoformat(), f"д{i}")
    assert not lab.digester.paths.days.exists(), "файлов дней нет — источником должна быть база"

    result = asyncio.run(lab.digester.make_week(date(2026, 7, 26)))

    assert result is not None and result.name == "2026-W30.md"
    assert lab.archive.window_rows()[0][1] == "2026-W30"
    body = lab.prompts[-1]
    for i in range(7):
        assert (date(2026, 7, 20) + timedelta(days=i)).isoformat() in body


def test_month_takes_its_own_days_not_weeks_that_started_in_it(lab):
    """Дефект 4: месяц собирался из недель, которые в нём начались.

    Неделя 29 июня – 5 июля числилась июньской: первых пяти дней июля в июльском
    месяце не было вовсе, зато были 1–2 августа.
    """
    for i in range((date(2026, 8, 5) - date(2026, 6, 25)).days + 1):
        day = date(2026, 6, 25) + timedelta(days=i)
        lab.archive._add_digest("day", day.isoformat(), f"день {day}")

    result = asyncio.run(lab.digester.make_month(date(2026, 7, 15)))

    assert result is not None and result.name == "2026-07.md"
    body = lab.prompts[-1]
    assert "2026-07-01" in body and "2026-07-31" in body, "месяц не знает своих краёв"
    assert "2026-06-30" not in body and "2026-08-01" not in body, "месяц забрал чужие дни"


def test_rollup_survives_when_all_files_are_deleted(lab):
    """Чеклист этапа: удалить все файлы выжимок → уровни собираются из базы."""
    for i in range(60):
        lab.live_a_day(date(2026, 6, 1) + timedelta(days=i))
    for path in lab.brain.rglob("*.md"):
        path.unlink()

    assert asyncio.run(lab.digester.make_week(date(2026, 7, 26))) is not None
    assert asyncio.run(lab.digester.make_month(date(2026, 7, 15))) is not None


def test_rollup_of_a_single_day_is_not_a_rollup(lab):
    lab.archive._add_digest("day", "2026-07-20", "один день")
    assert asyncio.run(lab.digester.make_week(date(2026, 7, 26))) is None


# --- журнал в мозге ---


def test_journal_keeps_the_same_window_the_coach_reads(lab):
    for i in range(40):
        lab.night(date(2026, 6, 1) + timedelta(days=i))

    assert len(list(lab.digester.paths.days.glob("*.md"))) == WINDOW["day"]
    assert len(list(lab.digester.paths.weeks.glob("*.md"))) <= WINDOW["week"]
    # Свежие дни на месте, старые остались только в базе и в истории git.
    assert (lab.digester.paths.days / "2026-07-10.md").exists()
    assert not (lab.digester.paths.days / "2026-06-01.md").exists()
    assert lab.archive.day_digests("2026-06-01", "2026-06-01"), "строка в базе пропала"


# --- год жизни ---


def test_a_year_of_nights_keeps_the_window_steady(lab):
    """Год подряд: количество кусков не пляшет и в покрытии нет дырок.

    Разгон — четыре месяца: пока не накопились три полных месяца и четыре полных
    недели, окно и не должно быть полным.
    """
    start = date(2026, 1, 1)
    warm_up = date(2026, 5, 1)
    sizes: set[int] = set()
    gaps: dict[date, list[date]] = {}

    for i in range((date(2026, 12, 31) - start).days + 1):
        day = start + timedelta(days=i)
        lab.night(day)
        if day < warm_up:
            continue
        rows = lab.archive.window_rows()
        sizes.add(len(rows))
        missing = holes(rows, day)
        if missing:
            gaps[day] = missing

    assert sizes == {sum(WINDOW.values())}, f"размер окна плавает: {sorted(sizes)}"
    assert not gaps, "в покрытии дырки: " + "; ".join(
        f"{day} не знает {[d.isoformat() for d in missing]}" for day, missing in list(gaps.items())[:5]
    )


# --- заметка, а не просто файл ---


def test_digest_files_are_notes_with_frontmatter(lab):
    """Заголовки заметок писались руками в этапе 12, а файлы пишет код.

    Без этого первая же ночная выжимка легла бы в мозг заметкой без заголовка,
    и валидатор стандарта посчитал бы её битой.
    """
    for i in range(7):
        lab.archive._add_digest("day", (date(2026, 7, 20) + timedelta(days=i)).isoformat(), f"д{i}")
    week = asyncio.run(lab.digester.make_week(date(2026, 7, 26)))

    head = week.read_text(encoding="utf-8")
    assert head.startswith("---\n")
    assert "title: 2026-W30" in head
    assert "type: source" in head, "выжимка не утверждает, а фиксирует — это источник"
    assert "created: 2026-07-27" in head, "дата сборки — из календаря, а не из часов машины"
    assert "root_id: [разговор-2026-07-20," in head, "корни — дни, из которых собрали"


def test_rotation_moves_the_index_links_with_the_files(lab):
    """Удалить файл и оставить ссылку на него — значит завести битую ссылку."""
    index = lab.make_index()
    for i in range(20):
        lab.night(date(2026, 6, 1) + timedelta(days=i))

    body = index.read_text(encoding="utf-8")
    assert "[[2026-06-20]]" in body, "свежий день не попал в точку входа"
    assert "[[2026-06-01]]" not in body, "ссылка осталась на удалённый файл"
    assert "[[2026-W24]]" in body and "— недели" in body
    assert body.count(INDEX_START) == 1 and body.count(INDEX_END) == 1
    assert "## Дальше" in body, "код затёр чужую часть файла"


def test_index_without_marks_is_left_alone(lab):
    index = lab.brain / "память" / "00-index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("# Точка входа\n\nручной список\n", encoding="utf-8")
    lab.night(date(2026, 6, 1))
    assert index.read_text(encoding="utf-8") == "# Точка входа\n\nручной список\n"
