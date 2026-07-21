"""Закладка в разговоре: какой session_id продолжать.

Файл переживает рестарт контейнера — в этом весь смысл непрерывного диалога.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)


class SessionStorage:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> str | None:
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def save(self, session_id: str) -> None:
        if self.load() != session_id:
            log.info("сессия: %s", session_id)
        self.path.write_text(session_id, encoding="utf-8")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
