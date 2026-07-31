"""Дашборд курса — один самодостаточный HTML-файл, который бот шлёт в телеграм.

Решение владельца 31.07 вместо дашборда на сервере: айфон открывает такой файл
во внутреннем браузере, ничего не надо хостить и не надо пароля.

**Ноль внешних запросов** — не про красоту. Из РФ половина CDN недоступна:
внешний шрифт не приедет, и вёрстка развалится ровно на телефоне, где смотреть
и собирались. Поэтому стили внутрь, колесо — inline SVG, ни одного `<script src>`.

**Собирает код, а не модель.** Правило, купленное трижды за этап 13: то, что
обязано случаться, делает код. Модель даёт содержание (миссию, горизонты,
оценки колеса — они лежат в мозге её рукой), форму даёт код.

Содержание утверждено владельцем до написания кода — «курс сверху, приборы
снизу», подробности в `.bpd/stages/16-strategicheskij-kontur/ДАШБОРД.md`.

**Пустое место — не ошибка.** Слой наполняется разговором, а не этапом. Нет
файла миссии — блок говорит «пока не заполнено» и страница не ломается.
"""

from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from math import cos, pi, sin
from pathlib import Path

import yaml

from coach_todoist_mcp.client import TodoistClient, TodoistError

log = logging.getLogger(__name__)

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)

# Приборы панели: у каждого свой вопрос и своё исправное состояние.
# Порог «В игре» намеренно не назначен — считается по данным через год.
GAUGES = (
    ("Потеряшки", "@актив & no date", "пусто"),
    ("Спящие цели", "@цель* & no date", "пусто"),
    ("Ждёт ответа", "@ждёт & (overdue | no date)", "пусто"),
    ("Inbox", "#Inbox", "0"),
    ("Просрочено", "overdue", "пусто"),
    ("В игре", "@актив & !no date", "—"),
)


@dataclass
class Данные:
    """Всё, что попадает на страницу. Пустое поле рисуется как «пока не заполнено»."""

    миссия: str = ""
    черновики: list[str] = field(default_factory=list)
    горизонты: dict[str, str] = field(default_factory=dict)
    цели: list[dict] = field(default_factory=list)
    колесо: list[tuple[str, dict]] = field(default_factory=list)
    приборы: list[tuple[str, int, str]] = field(default_factory=list)
    период: dict[str, int] = field(default_factory=dict)


# ── чтение мозга ─────────────────────────────────────────────────────────────

def _черновик(path: Path) -> bool:
    """Заметка ещё не подтверждена человеком (`status: draft` в шапке)."""
    if not path.exists():
        return False
    head = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    return bool(head and re.search(r"^status:\s*draft\s*$", head.group(1), re.M))


def _тело(path: Path) -> str:
    """Содержательная часть заметки: без шапки, заголовка и служебных врезок.

    **Цитаты (`>`) на дашборд не идут.** В этом мозге блок-цитата — всегда
    служебное: закон модуля, объяснение валидатору, пометка «это черновик».
    Проверено вживую 31.07: первая версия вывалила на телефон рассуждение
    про `consensus: single` вместо миссии. Человеку нужно содержание,
    а разговор с валидатором пусть остаётся в файле.
    """
    if not path.exists():
        return ""
    текст = FRONTMATTER.sub("", path.read_text(encoding="utf-8"))
    строки = [
        s for s in текст.splitlines()
        if not s.startswith("# ") and not s.lstrip().startswith(">")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(строки)).strip()


def _суть(path: Path) -> str:
    """Только первый содержательный раздел заметки — то, что человек хочет видеть.

    Дашборд — витрина, а не читалка файлов. В заметке миссии после сути идут
    таблица «откуда взято и что здесь домысел», раздел «что спросить», связи
    и история обновлений: всё это рабочий материал агента. Владелец открыл файл
    на телефоне и увидел именно их — значит выводить надо не файл, а суть.

    Берём тело первого `##`-раздела; нет заголовков — берём всё.
    """
    текст = _тело(path)
    разделы = re.split(r"^##\s+.*$", текст, flags=re.M)
    for кусок in разделы:
        if кусок.strip():
            return кусок.strip()
    return текст


def _горизонты(path: Path) -> dict[str, str]:
    """Разделы ГОД / КВАРТАЛ / МЕСЯЦ. Ищем по заголовку, а не по порядку."""
    текст = _тело(path)
    if not текст:
        return {}
    куски: dict[str, str] = {}
    текущий = ""
    for строка in текст.splitlines():
        заголовок = re.match(r"^##\s+(ГОД|КВАРТАЛ|МЕСЯЦ)\b(.*)$", строка.strip())
        if заголовок:
            текущий = заголовок.group(1)
            куски[текущий] = заголовок.group(2).strip() + "\n"
        elif текущий:
            куски[текущий] += строка + "\n"
    return {k: v.strip() for k, v in куски.items() if v.strip()}


def _колесо(path: Path) -> list[tuple[str, dict]]:
    """Ряд замеров из YAML-блока. Формат по читателю: файл читает КОД."""
    if not path.exists():
        return []
    блок = YAML_BLOCK.search(path.read_text(encoding="utf-8"))
    if not блок:
        return []
    try:
        данные = yaml.safe_load(блок.group(1)) or {}
    except yaml.YAMLError as err:
        log.warning("колесо баланса не разбирается: %s", err)
        return []
    if not isinstance(данные, dict):
        return []
    ряды = [(str(k), v) for k, v in данные.items() if isinstance(v, dict)]
    return sorted(ряды)[-6:]  # последние полгода: линия видна, страница не пухнет


# ── чтение Todoist ───────────────────────────────────────────────────────────

async def _счёт(client: TodoistClient, query: str) -> list[dict]:
    try:
        data = await client.get("/tasks/filter", params={"query": query, "limit": 100})
    except TodoistError as err:
        log.warning("прибор «%s» не снялся: %s", query, err)
        return []
    return data.get("results", []) if isinstance(data, dict) else (data or [])


async def _цели(client: TodoistClient) -> list[dict]:
    """Работа по каждой метке @цель-*: сколько карточек и есть ли следующий шаг.

    Норма машинная ровно поэтому: «у карточки с меткой цели есть шаг с датой»
    код проверить может, а по тексту в описании — нет.
    """
    try:
        labels = await client.get_paginated("/labels", params={"limit": 200})
    except TodoistError as err:
        log.warning("метки не забрались: %s", err)
        return []

    цели = []
    for label in labels:
        имя = (label.get("name") or "")
        if not имя.startswith("цель-"):
            continue
        задачи = await _счёт(client, f"@{имя}")
        сроки = sorted(
            (t.get("due") or {}).get("date", "")[:10] for t in задачи if (t.get("due") or {}).get("date")
        )
        цели.append({
            "имя": имя.removeprefix("цель-"),
            "всего": len(задачи),
            "с_датой": len(сроки),
            "ближайший": сроки[0] if сроки else "",
        })
    return sorted(цели, key=lambda c: c["имя"])


# ── чтение архива ────────────────────────────────────────────────────────────

def _период(db_path: Path, дней: int) -> dict[str, int]:
    """Сколько закрыто за период. Считает снимок, а не журнал Todoist:
    на Free журнал живёт около недели, а снимок помнит всё."""
    if not db_path.exists():
        return {}
    порог = (date.today() - timedelta(days=дней)).isoformat()
    try:
        with sqlite3.connect(db_path) as db:
            строка = db.execute(
                "SELECT count(*) FROM todoist_closed WHERE completed_at >= ?", (порог,)
            ).fetchone()
    except sqlite3.Error as err:
        log.warning("архив закрытых не читается: %s", err)
        return {}
    return {"закрыто": строка[0] if строка else 0, "дней": дней}


# ── сборка данных ────────────────────────────────────────────────────────────

async def собрать(brain_dir: Path, db_path: Path, token: str, дней: int = 7) -> Данные:
    память = brain_dir / "память"
    черновики = [
        имя for имя, путь in (
            ("миссия", память / "знания" / "миссия.md"),
            ("ценности", память / "знания" / "ценности.md"),
            ("горизонты", память / "состояние" / "горизонты.md"),
        ) if _черновик(путь)
    ]
    данные = Данные(
        миссия=_суть(память / "знания" / "миссия.md"),
        черновики=черновики,
        горизонты=_горизонты(память / "состояние" / "горизонты.md"),
        колесо=_колесо(память / "журнал" / "колесо-баланса.md"),
        период=_период(db_path, дней),
    )
    try:
        async with TodoistClient(token) as client:
            данные.цели = await _цели(client)
            данные.приборы = [
                (имя, len(await _счёт(client, запрос)), норма) for имя, запрос, норма in GAUGES
            ]
    except (TodoistError, OSError) as err:
        log.error("Todoist для дашборда недоступен: %s", err)
    return данные


# ── рисование ────────────────────────────────────────────────────────────────

def _э(текст: str) -> str:
    return html.escape(текст or "")


ЖИРНОЕ = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
КОД = re.compile(r"`([^`]+)`")


def _строка(текст: str) -> str:
    """Экранируем, потом отрисовываем жирное и код. Порядок именно такой:
    сначала гасим чужой HTML, и только потом ставим свой."""
    готово = _э(текст)
    готово = ЖИРНОЕ.sub(r"<strong>\1</strong>", готово)
    return КОД.sub(r"<code>\1</code>", готово)


def _абзацы(текст: str) -> str:
    """Абзацы, списки и подзаголовки. Не полный markdown — ровно то, чем
    написаны заметки курса: `**жирное**`, `` `код` ``, списки на дефисах, `###`.
    Иначе на телефоне видны сами звёздочки, а не жирный шрифт (проверено вживую)."""
    if not текст:
        return '<p class="пусто">Пока не заполнено — проговорим в разговоре.</p>'

    куски = []
    for блок in (k.strip() for k in текст.split("\n\n")):
        if not блок:
            continue
        строки = блок.splitlines()
        if all(s.lstrip().startswith(("- ", "* ")) for s in строки):
            пункты = "".join(f"<li>{_строка(s.lstrip()[2:])}</li>" for s in строки)
            куски.append(f"<ul>{пункты}</ul>")
        elif all(s.strip().startswith("|") for s in строки):
            куски.append(_таблица(строки))
        elif блок.startswith("#"):
            голова, _, хвост = блок.partition("\n")
            куски.append(f"<h4>{_строка(голова.lstrip('# '))}</h4>")
            if хвост.strip():
                куски.append(_абзацы(хвост.strip()))
        else:
            # Одиночный перенос строки — тот же абзац, а не разрыв. Иначе текст
            # рвётся посередине предложения там, где в файле кончилась строка.
            куски.append("<p>" + _строка(" ".join(s.strip() for s in строки)) + "</p>")
    return "".join(куски)


def _таблица(строки: list[str]) -> str:
    """Markdown-таблица в HTML. Без этого на телефоне видны сами палки и дефисы."""
    ряды = [
        [я.strip() for я in s.strip().strip("|").split("|")]
        for s in строки
        if not re.fullmatch(r"\|[\s|:-]+\|", s.strip())
    ]
    if not ряды:
        return ""
    шапка = "".join(f"<th>{_строка(я)}</th>" for я in ряды[0])
    тело = "".join(
        "<tr>" + "".join(f"<td>{_строка(я)}</td>" for я in ряд) + "</tr>" for ряд in ряды[1:]
    )
    return f'<div class="обёртка"><table><thead><tr>{шапка}</tr></thead><tbody>{тело}</tbody></table></div>'


def _колесо_svg(ряды: list[tuple[str, dict]]) -> str:
    """Колесо последнего замера. Inline SVG: картинка без единого запроса наружу."""
    if not ряды:
        return '<p class="пусто">Замеров ещё нет — первый сделаем на месячном итоге.</p>'

    _, последний = ряды[-1]
    сферы = [(k, v) for k, v in последний.items() if isinstance(v, (int, float))]
    if not сферы:
        return '<p class="пусто">В последнем замере нет чисел.</p>'

    R, центр, макс = 90, 110, 5
    кольца = "".join(
        f'<circle cx="{центр}" cy="{центр}" r="{R * i / макс:.1f}" class="кольцо"/>'
        for i in range(1, макс + 1)
    )
    точки, подписи = [], []
    for i, (имя, оценка) in enumerate(сферы):
        угол = -pi / 2 + 2 * pi * i / len(сферы)
        r = R * min(float(оценка), макс) / макс
        точки.append(f"{центр + r * cos(угол):.1f},{центр + r * sin(угол):.1f}")
        подписи.append(
            f'<text x="{центр + (R + 16) * cos(угол):.1f}" y="{центр + (R + 16) * sin(угол):.1f}" '
            f'class="подпись" text-anchor="middle">{_э(имя)} {оценка}</text>'
        )
    return (
        f'<svg viewBox="0 0 220 220" class="колесо" role="img" '
        f'aria-label="колесо баланса">{кольца}'
        f'<polygon points="{" ".join(точки)}" class="фигура"/>{"".join(подписи)}</svg>'
    )


def _линия(ряды: list[tuple[str, dict]]) -> str:
    """Ряд замеров таблицей. Перекос — это не «сегодня четвёрка»,
    а «съезжает третий месяц подряд», и увидеть это можно только рядом."""
    if len(ряды) < 2:
        return ""
    сферы = list(ряды[-1][1].keys())
    шапка = "".join(f"<th>{_э(s)}</th>" for s in сферы)
    строки = "".join(
        f"<tr><th>{_э(месяц)}</th>"
        + "".join(f"<td>{_э(str(оценки.get(s, '—')))}</td>" for s in сферы)
        + "</tr>"
        for месяц, оценки in ряды
    )
    return f'<table class="ряд"><thead><tr><th>месяц</th>{шапка}</tr></thead><tbody>{строки}</tbody></table>'


def _цели_html(цели: list[dict]) -> str:
    if not цели:
        return '<p class="пусто">Меток @цель-* в Todoist ещё нет — работа со стратегией не размечена.</p>'
    строки = ""
    for ц in цели:
        спит = ц["с_датой"] == 0
        строки += (
            f'<tr class="{"тревога" if спит else ""}">'
            f'<th>{_э(ц["имя"])}</th>'
            f'<td>{ц["всего"]}</td><td>{ц["с_датой"]}</td>'
            f'<td>{_э(ц["ближайший"]) or "— спит"}</td></tr>'
        )
    return (
        '<table><thead><tr><th>цель</th><th>карточек</th><th>с датой</th>'
        f'<th>ближайший шаг</th></tr></thead><tbody>{строки}</tbody></table>'
    )


def _приборы_html(приборы: list[tuple[str, int, str]]) -> str:
    if not приборы:
        return '<p class="пусто">Todoist не ответил — приборы не сняты.</p>'
    плитки = ""
    for имя, факт, норма in приборы:
        исправен = норма == "—" or (норма in ("пусто", "0") and факт == 0)
        плитки += (
            f'<div class="плитка {"зелёная" if исправен else "красная"}">'
            f'<div class="цифра">{факт}</div><div class="имя">{_э(имя)}</div>'
            f'<div class="норма">норма: {_э(норма)}</div></div>'
        )
    return f'<div class="плитки">{плитки}</div>'


СТИЛИ = """
:root { color-scheme: light dark; --фон:#fff; --текст:#1a1a1a; --тихий:#666;
        --рамка:#e2e2e2; --акцент:#2f6f4f; --тревога:#b03030; --карточка:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --фон:#16181c; --текст:#e8e8e8; --тихий:#9aa0a6; --рамка:#2c2f36;
          --акцент:#6fbf8f; --тревога:#e07a7a; --карточка:#1d2027; }
}
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--фон); color:var(--текст);
       font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width:900px; margin-inline:auto; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:17px; margin:28px 0 10px; padding-bottom:6px;
     border-bottom:1px solid var(--рамка); }
h3 { font-size:15px; margin:16px 0 6px; color:var(--акцент); letter-spacing:.04em; }
.дата { color:var(--тихий); font-size:14px; margin:0 0 8px; }
.пусто { color:var(--тихий); font-style:italic; }
.черновик { background:var(--карточка); border:1px solid var(--тревога); color:var(--тревога);
            border-radius:8px; padding:8px 10px; font-size:13px; margin:0 0 14px; }
h4 { font-size:14px; margin:14px 0 4px; color:var(--тихий); text-transform:uppercase;
     letter-spacing:.04em; }
ul { margin:0 0 10px; padding-left:20px; }
li { margin-bottom:4px; }
code { background:var(--рамка); border-radius:4px; padding:1px 5px; font-size:13px; }
section { background:var(--карточка); border:1px solid var(--рамка);
          border-radius:12px; padding:14px 16px; margin-bottom:14px; }
p { margin:0 0 10px; }
.обёртка { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--рамка);
         vertical-align:top; }
.плитки + table th, .плитки + table td { white-space:nowrap; }
tbody th { font-weight:600; white-space:normal; }
tr.тревога td, tr.тревога th { color:var(--тревога); }
.плитки { display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); }
.плитка { border:1px solid var(--рамка); border-radius:10px; padding:10px; text-align:center; }
.плитка.зелёная { border-color:var(--акцент); }
.плитка.красная { border-color:var(--тревога); }
.цифра { font-size:26px; font-weight:700; line-height:1; }
.плитка.красная .цифра { color:var(--тревога); }
.плитка.зелёная .цифра { color:var(--акцент); }
.имя { font-size:14px; margin-top:4px; }
.норма { font-size:12px; color:var(--тихий); }
.колесо { width:100%; max-width:280px; height:auto; display:block; margin:0 auto; }
.кольцо { fill:none; stroke:var(--рамка); stroke-width:1; }
.фигура { fill:var(--акцент); fill-opacity:.28; stroke:var(--акцент); stroke-width:2; }
.подпись { font-size:9px; fill:var(--текст); }
footer { color:var(--тихий); font-size:12px; margin-top:22px; }
"""


def нарисовать(данные: Данные, день: date) -> str:
    горизонты = "".join(
        f"<h3>{_э(имя)}</h3>{_абзацы(данные.горизонты.get(имя, ''))}"
        for имя in ("ГОД", "КВАРТАЛ", "МЕСЯЦ")
    )
    плашка = (
        '<p class="черновик">⚠️ Черновик: ' + _э(", ".join(данные.черновики))
        + " — собрано агентом из журнала и Василием не подтверждено.</p>"
    ) if данные.черновики else ""
    период = данные.период
    период_html = (
        f"<p>Закрыто за последние {период['дней']} дн.: <strong>{период['закрыто']}</strong></p>"
        if период else '<p class="пусто">Снимков дел ещё нет — счёт появится через сутки.</p>'
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Курс · {день.isoformat()}</title>
<style>{СТИЛИ}</style></head><body>
<h1>Курс</h1>
<p class="дата">{день.strftime('%d.%m.%Y')}</p>
{плашка}

<section><h2>Миссия</h2>{_абзацы(данные.миссия)}</section>

<section><h2>Горизонты</h2>{горизонты}</section>

<section><h2>Работа по целям</h2><div class="обёртка">{_цели_html(данные.цели)}</div></section>

<section><h2>Колесо баланса</h2>{_колесо_svg(данные.колесо)}
<div class="обёртка">{_линия(данные.колесо)}</div></section>

<section><h2>Приборы</h2>{_приборы_html(данные.приборы)}</section>

<section><h2>Период</h2>{период_html}</section>

<footer>Собрано ботом-коучем. Смысл живёт в памяти, работа — в Todoist;
здесь только вид сверху.</footer>
</body></html>"""


async def собрать_файл(brain_dir: Path, db_path: Path, token: str,
                       куда: Path | None = None, дней: int = 7) -> Path:
    """Собрать дашборд и положить файлом. Возвращает путь.

    Имя с датой — чтобы вчерашний и сегодняшний не путались в чате.
    """
    день = date.today()
    данные = await собрать(brain_dir, db_path, token, дней)
    папка = куда or Path(os.environ.get("DASHBOARD_DIR", "/tmp"))
    папка.mkdir(parents=True, exist_ok=True)
    путь = папка / f"курс-{день.isoformat()}.html"
    путь.write_text(нарисовать(данные, день), encoding="utf-8")
    log.info("дашборд собран: %s (%d Б)", путь, путь.stat().st_size)
    return путь
