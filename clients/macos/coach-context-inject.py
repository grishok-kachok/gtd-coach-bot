#!/usr/bin/env python3
"""SessionStart-хук: программно грузит выжимки памяти коуча в контекст сессии.

Та же 30-дневная память, что бот на сервере подставляет себе в новый разговор:
месяцы → недели → дни из память/журнал/выжимки/. Папки самоограничены — старые
дни сворачиваются в недели и удаляются, так что объём не растёт бесконечно.
Никакой магии с правилами в CLAUDE.md: контекст кладётся кодом, всегда.
"""

from __future__ import annotations  # хук может запускаться системным python 3.9

import json
from pathlib import Path

BASE = Path("/Users/vasiliy/Yandex.Disk.localized/Claude Code/gtd/память/журнал/выжимки")
CAP = 80_000  # символов; при переборе жертвуем самым старым


def main() -> None:
    parts: list[str] = []
    for sub in ("месяцы", "недели", "дни"):
        folder = BASE / sub
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                parts.append(text)
    if not parts:
        return
    while parts and sum(len(p) for p in parts) > CAP:
        parts.pop(0)
    context = (
        "Выжимки прошлых дней из памяти коуча (общая память с телеграм-ботом; "
        "подгружено автоматически при старте сессии). Это фон для работы: "
        "опирайся на него, не пересказывай владельцу без нужды.\n\n"
        + "\n\n---\n\n".join(parts)
    )
    print(json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
