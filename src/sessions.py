"""Закладка в разговоре: какой session_id продолжать.

Файл переживает рестарт контейнера — в этом весь смысл непрерывного диалога.

С правки 02.08.2026 закладок может быть две. Стратсессия идёт **отдельным
заходом**: текущий разговор откладывается вместе со своим режимом, обряд
идёт со своей головой, а по окончании человек возвращается ровно туда, где
был. Не «начать заново», а «отложить книжку с закладкой, сходить в другую
комнату и вернуться».

Старая сессия при этом никуда не девается: движок хранит её у себя на диске,
и `resume` по отложенному id поднимает её как ни в чём не бывало.
"""

from __future__ import annotations

import json
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

    # --- отложенный разговор ---

    def _shelf_path(self) -> Path:
        return self.path.with_name(self.path.name + "_отложено")

    def отложить(self) -> bool:
        """Убрать текущий разговор на полку. Возвращает, было ли что убирать.

        Полка одна: обряд внутри обряда — это не сценарий, а способ потерять
        оба разговора. Второе откладывание поверх первого не делается.
        """
        сессия = self.load()
        if сессия is None:
            self.clear()
            return False
        if self.отложено():
            log.warning("на полке уже лежит разговор — второй туда не кладу")
            return False
        self._shelf_path().write_text(
            json.dumps({"session_id": сессия, "mode": self.mode()}, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("разговор %s отложен на полку", сессия)
        self.clear()
        return True

    def отложено(self) -> dict | None:
        """Что лежит на полке. Пусто — там ничего нет."""
        try:
            данные = json.loads(self._shelf_path().read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return данные if isinstance(данные, dict) and данные.get("session_id") else None

    def вернуть(self) -> str | None:
        """Снять разговор с полки и продолжить его. Возвращает session_id."""
        данные = self.отложено()
        self._shelf_path().unlink(missing_ok=True)
        if данные is None:
            self.clear()
            return None
        self.save(str(данные["session_id"]))
        if данные.get("mode"):
            self.save_mode(str(данные["mode"]))
        log.info("разговор %s снят с полки", данные["session_id"])
        return str(данные["session_id"])
