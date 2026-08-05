"""Накопительная заметка мозга: заголовок один раз, дальше только дописываем.

Форма одна: файл-заметка со стандартным заголовком, а внутри разделы по дням.
Держать её в нескольких местах значило бы завести несколько домов у одного
правила: поправишь в одном — забудешь в остальных.

Тип — `source`. Такая заметка ничего не утверждает про пользователя, она
фиксирует замеченное.

**Три ночных копилки отсюда уехали** (этап 20, 05.08.2026): заявки, предложения
памяти и промахи живут теперь на полке `inbox` в базе, потому что «разобрано»
у них обязано быть полем, а не зачёркиванием руками. Остался журнал замеров
профиля — ему накопительный текст подходит: его читают глазами и подряд,
а разбирать по одной записи там нечего.
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
