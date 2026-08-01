"""Настройки коуча: какой моделью думать и какие кнопки грузить.

Дом — мозг, а не `.env` на сервере. Та же болезнь, что была у ритмов до
переезда: настройка про самого человека («думай моделью помощнее», «убери
статистику, она мне не нужна») лежала там, куда он дотянуться не может —
нужен ssh и перезапуск. Теперь это данные, и меняются они фразой в телеграме.

Формат — строгий YAML со схемой, по тому же правилу, что и ритмы: файл
читает КОД. Опечатка в прозе означала бы, что бот молча думает не той
моделью; опечатка в YAML со схемой — жалоба сразу и вслух, а настройки
остаются прежними, пока файл не починят.

Ключи по-русски намеренно: файл правят человек и коуч, а не программист.

**Смена модели применяется со следующей реплики.** Движок живёт ровно один
ответ (`engine.py`, `async with ClaudeSDKClient`), а нить разговора держит
не живой процесс, а `resume=session_id`. Поэтому перечитать настройки перед
ответом достаточно: ни перезапуска, ни потери разговора.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from .modes import ПО_УМОЛЧАНИЮ as РЕЖИМ_ПО_УМОЛЧАНИЮ, РАЗГОВОРНЫЕ

log = logging.getLogger(__name__)

SETTINGS_FILE = "настройки.md"

# Модели, которые код готов принять. Список тут не для красоты: опечатка
# в имени модели превращается в бота, который падает на каждом ответе,
# и понять почему по логам трудно.
KNOWN_MODELS = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-haiku-4-5-20251001",
)

DEFAULTS: dict[str, object] = {
    "модель_разговора": "claude-fable-5",
    "модель_ночной_работы": "claude-fable-5",
    "режим": РЕЖИМ_ПО_УМОЛЧАНИЮ,
    "кнопки_todoist": {},
    "кнопки_календаря": {},
}

YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


def known_button_names() -> tuple[set[str], set[str]]:
    """Имена кнопок из манифестов — чтобы поймать опечатку в тумблере.

    Пакеты приезжают смонтированными и в тестах могут отсутствовать; тогда
    проверять не с чем, и лишние имена просто не ловятся.
    """
    try:
        from todoist_mcp import tools as todoist_tools
        todoist = {t.name for t in todoist_tools.TOOLS}
    except Exception:
        todoist = set()
    try:
        from gcal_mcp import tools as calendar_tools
        calendar = {t.name for t in calendar_tools.TOOLS}
    except Exception:
        calendar = set()
    return todoist, calendar


def _complaints(data: dict) -> list[str]:
    """Претензии к файлу человеческим языком. Пусто — настройки годные."""
    problems: list[str] = []

    for key in ("модель_разговора", "модель_ночной_работы"):
        if key not in data:
            problems.append(f"нет строки «{key}»")
            continue
        value = data[key]
        if value not in KNOWN_MODELS:
            problems.append(
                f"«{key}: {value}» — код не знает такой модели. Доступны: "
                + ", ".join(KNOWN_MODELS)
            )

    if "режим" in data and data["режим"] not in РАЗГОВОРНЫЕ:
        # Молча проглоченная опечатка означала бы, что человек попросил полный
        # режим, а коуч продолжил жить в рабочем и об этом не сказал.
        problems.append(
            f"«режим: {data['режим']}» — такого режима разговора нет. Доступны: "
            + ", ".join(РАЗГОВОРНЫЕ)
        )

    todoist_names, calendar_names = known_button_names()
    for key, known in (("кнопки_todoist", todoist_names), ("кнопки_календаря", calendar_names)):
        if key not in data:
            continue  # раздел необязателен: нет его — все кнопки включены
        section = data[key]
        if section is None:
            continue
        if not isinstance(section, dict):
            problems.append(f"«{key}» — нужен список строк «имя_кнопки: on» или «off»")
            continue
        for name, value in section.items():
            if not isinstance(value, bool):
                problems.append(f"«{key} → {name}: {value}» — нужно on или off")
            elif known and name not in known:
                # Молча проглоченная опечатка означала бы, что человек думает,
                # будто кнопку выключил, а она грузится.
                problems.append(f"«{key} → {name}» — такой кнопки нет")

    unknown = set(data) - set(DEFAULTS)
    if unknown:
        problems.append("лишние строки, которых код не знает: " + ", ".join(sorted(unknown)))
    return problems


def path_in(brain_dir: Path) -> Path:
    return brain_dir / "память" / "состояние" / SETTINGS_FILE


def read(brain_dir: Path) -> tuple[dict, list[str]]:
    """Настройки и претензии к файлу.

    Претензии есть — отдаём УМОЛЧАНИЯ целиком, а не половину файла: настройки,
    собранные из годных строк и подставленных умолчаний, выглядят рабочими
    и врут про то, чем бот сейчас думает.
    """
    path = path_in(brain_dir)
    if not path.exists():
        return dict(DEFAULTS), []

    block = YAML_BLOCK.search(path.read_text(encoding="utf-8"))
    if not block:
        return dict(DEFAULTS), ["в файле нет блока ```yaml — настройки читать неоткуда"]
    try:
        data = yaml.safe_load(block.group(1))
    except yaml.YAMLError as err:
        return dict(DEFAULTS), [f"YAML не разбирается: {err}"]
    if not isinstance(data, dict):
        return dict(DEFAULTS), ["в блоке ```yaml должен быть список строк «ключ: значение»"]

    problems = _complaints(data)
    if problems:
        return dict(DEFAULTS), problems

    result = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in data and data[key] is not None:
            result[key] = data[key]
    return result, []


TEMPLATE = """---
title: настройки
type: состояние
schema_version: "1.0"
status: stable
created: {today}
as_of: {today}
temporal: true
tags: [инструменты]
---

# Настройки: чем коуч думает и какими кнопками пользуется

> Живой модуль **состояние**: всегда актуально, перезаписывается на месте.

Этот файл читает **код бота**, а не только модель. Поэтому блок ниже — строгий
YAML: имя модели из известного списка, тумблеры — `on` или `off`. Сломаешь
формат — бот не станет угадывать, а напишет, что файл не читается, и продолжит
работать на прежних настройках. Половина файла хуже отсутствия файла: настройки
из годных строк и подставленных умолчаний выглядят рабочими и врут про то,
чем бот сейчас думает.

Поменять можно прямо в разговоре: «переключись на Opus», «выключи статистику» —
коуч правит этот файл, и со следующей реплики всё уже по-новому. Перезапуск
не нужен, `.env` ни при чём.

```yaml
модель_разговора: "{talk}"
модель_ночной_работы: "{night}"
режим: "{mode}"
кнопки_todoist: {{}}
кнопки_календаря: {{}}
```

- **модель_разговора** — кем коуч отвечает в телеграме. Доступны: {models}.
- **модель_ночной_работы** — кем он ночью сворачивает день и проверяет память.
  Ночью торопиться некуда, а объём большой, поэтому модель тут может быть
  другой, чем в разговоре.
- **режим** — сколько коуч берёт с собой в разговор. Доступны: {modes_list}.
  `рабочий` — обычный день. `полный` — недельный обзор и месячный итог,
  туда приезжают миссия, ценности и профиль. `годовой` — раз в год, там же
  двенадцать месяцев памяти вместо трёх. Состав каждого — файл `режимы.md`
  в плагине. Смена режима **закрывает текущий разговор и открывает новый**:
  память подкладывается только в первый запрос сессии, и по-другому
  переключение не значило бы ничего.
- **кнопки_todoist / кнопки_календаря** — что не грузить в сессию.
  Пустой список `{{}}` значит «грузить всё». Выключенная кнопка не просто
  не работает — она не занимает места в контексте, а место здесь и есть
  вся причина. Список кнопок и их назначение — `инструменты`.

Кнопка, которой в списке нет, считается **включённой**: приехала новая
возможность — она работает сразу. Молча пропавшую кнопку ищут неделями,
лишнюю выключают за десять секунд.
"""

INDEX_LINE = ("- [[настройки]] — чем коуч думает и какие кнопки грузит; "
              "файл читает код, формат строгий\n")


def ensure(brain_dir: Path, today: str) -> bool:
    """Завести файл настроек и ссылку на него, если их ещё нет.

    Файл читает код — значит код и обязан обеспечить, что файл существует.
    Ссылка ставится в тот же заход: заметка без ссылки — сирота для валидатора,
    ссылка без заметки — битая (решение проекта от 31.07).

    Возвращает True, если что-то создано.
    """
    created = False
    path = path_in(brain_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            TEMPLATE.format(today=today, talk=DEFAULTS["модель_разговора"],
                            night=DEFAULTS["модель_ночной_работы"],
                            mode=DEFAULTS["режим"],
                            models=", ".join(f"`{m}`" for m in KNOWN_MODELS),
                            modes_list=", ".join(f"`{m}`" for m in РАЗГОВОРНЫЕ)),
            encoding="utf-8",
        )
        created = True
    else:
        created = _дописать_режим(path) or created

    index = brain_dir / "память" / "00-index.md"
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return created
    if "[[настройки]]" in text:
        return created
    # Ставим рядом с ритмами: соседний файл того же рода — настройка, которую
    # читает код, а меняет человек разговором.
    anchor = "- [[ритмы]]"
    position = text.find(anchor)
    if position == -1:
        return created
    end = text.find("\n", position) + 1
    index.write_text(text[:end] + INDEX_LINE + text[end:], encoding="utf-8")
    return True


def _дописать_режим(path: Path) -> bool:
    """Дотянуть уже заведённый файл до нынешней схемы: строка «режим».

    Файл читает код — значит и держать его полным работа кода. Без этой
    строки режим существовал бы только в питоне: человек открыл бы настройки
    и не узнал, что переключаться вообще можно. Правка добавляющая, чужого
    не трогает.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    block = YAML_BLOCK.search(text)
    if not block or re.search(r"^\s*режим\s*:", block.group(1), re.MULTILINE):
        return False
    строка = (f'режим: "{DEFAULTS["режим"]}"   # {" | ".join(РАЗГОВОРНЫЕ)}'
              f' — состав в режимы.md плагина\n')
    # Ставим сразу за моделями: соседняя настройка того же рода — её меняют
    # словами в разговоре, а читает код.
    вставка = block.start(1) + len(block.group(1))
    хвост = block.group(1).rstrip("\n")
    новый = f"{хвост}\n{строка.rstrip()}"
    path.write_text(text[:block.start(1)] + новый + text[вставка:], encoding="utf-8")
    log.info("в настройки дописана строка «режим»: %s", path)
    return True


def describe(settings: dict, previous: dict | None = None) -> str:
    """Человеческая строка про настройки — её коуч показывает человеку."""
    def buttons(value: dict) -> str:
        off = sorted(name for name, on in (value or {}).items() if not on)
        return "все" if not off else f"все, кроме: {', '.join(off)}"

    if previous is None:
        return (f"разговор — {settings['модель_разговора']}, "
                f"ночная работа — {settings['модель_ночной_работы']}, "
                f"режим — {settings['режим']}, "
                f"кнопки Todoist: {buttons(settings['кнопки_todoist'])}, "
                f"кнопки Календаря: {buttons(settings['кнопки_календаря'])}")
    changed = []
    for key in ("модель_разговора", "модель_ночной_работы", "режим"):
        if settings[key] != previous[key]:
            changed.append(f"{key.replace('_', ' ')}: {previous[key]} → {settings[key]}")
    for key in ("кнопки_todoist", "кнопки_календаря"):
        if settings[key] != previous[key]:
            changed.append(f"{key.replace('_', ' ')}: {buttons(previous[key])} → "
                           f"{buttons(settings[key])}")
    return ", ".join(changed) if changed else "ничего не изменилось"
