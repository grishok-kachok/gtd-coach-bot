"""Накопительная заметка мозга: заголовок один раз, дальше только дописываем.

Так устроены три ночных копилки — предложения памяти, промахи памяти и заявки.
У всех одна форма: файл-заметка со стандартным заголовком, а внутри разделы
по дням. Держать эту форму в трёх местах значило бы завести три дома у одного
правила: поправишь в одном — забудешь в двух.

Тип у всех — `source`. Копилка ничего не утверждает про Василия, она фиксирует
замеченное: предложение ещё не знание, промах — событие, заявка — просьба.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

HEAD = """---
title: {title}
type: source
schema_version: "1.0"
status: stable
created: {created}
source_type: personal-experience
reliability: C
author: {author}
ref: {ref}
root_id: [{root}]
tags: [{tags}]
---

# {heading}

> {note}

"""


def append(path: Path, *, title: str, heading: str, note: str, ref: str, author: str,
           tags: str, day: date, block: str) -> Path:
    """Дописать блок в копилку, заведя её при первом обращении."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            HEAD.format(title=title, created=day.isoformat(), author=author, ref=ref,
                        root=f"разговор-{day.isoformat()}", heading=heading, note=note,
                        tags=tags),
            encoding="utf-8",
        )
    with path.open("a", encoding="utf-8") as f:
        f.write(block)
    return path
