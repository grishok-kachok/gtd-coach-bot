"""Импорт реплик из VS Code (Claude Code) в архив разговоров.

Макбук выжимает из транскриптов Claude Code только реплики Василия и текстовые
ответы коуча и шлёт их сюда по ssh: `docker exec -i coach-bot python -m
src.import_vscode` с JSON-строками на stdin. Формат строки:
    {"uuid": "...", "ts": "2026-07-22T14:03:11.000Z", "role": "vasiliy"|"coach", "text": "..."}

Дубли отсеиваются уникальным индексом по uuid — импорт можно гонять сколько
угодно раз, база не распухнет. Время переводится в московское, чтобы реплика
легла в правильный день ночной выжимки.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .archive import Archive

MOSCOW = ZoneInfo("Europe/Moscow")


def main() -> None:
    archive = Archive(Path(os.environ.get("ARCHIVE_DB", "/archive/coach.db")))
    added = doubles = bad = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            uuid = str(item["uuid"])
            role = str(item["role"])
            text = str(item["text"]).strip()
            ts = datetime.fromisoformat(str(item["ts"]).replace("Z", "+00:00")).astimezone(MOSCOW)
        except (KeyError, ValueError):
            bad += 1
            continue
        if role not in ("vasiliy", "coach") or not text:
            bad += 1
            continue
        if archive.import_message(uuid, ts, role, text):
            added += 1
        else:
            doubles += 1
    print(f"добавлено: {added}, дубликатов: {doubles}, битых: {bad}")


if __name__ == "__main__":
    main()
