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
        self._mode_path().unlink(missing_ok=True)

    # --- режим, в котором собран этот разговор ---
    #
    # Хранится рядом с закладкой и по той же причине: он должен пережить
    # рестарт контейнера. Иначе после каждого перезапуска бот считал бы, что
    # режим сменился, и рвал бы разговор на ровном месте.

    def _mode_path(self) -> Path:
        return self.path.with_name(self.path.name + "_mode")

    def mode(self) -> str | None:
        try:
            value = self._mode_path().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return value or None

    def save_mode(self, name: str) -> None:
        self._mode_path().write_text(name, encoding="utf-8")
