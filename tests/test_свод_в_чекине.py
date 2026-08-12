"""Свод обязан доехать до утреннего чек-ина.

Куплено 12.08.2026: `_with_brief` осталась `@staticmethod`, а внутрь добавили
`self.snapshot.db_path` — утренний чек-ин упал на `NameError`, и сообщения
в 10:00 просто не было. Прогон был зелёный: подкладку свода никто не звал.
Тест зовёт её так же, как зовёт бот, — от лица объекта.
"""

from pathlib import Path
from types import SimpleNamespace

from src import detectors, main


def _бот(db_path: Path):
    """Бот, урезанный до того, что нужно подкладке: путь к базе."""
    return SimpleNamespace(snapshot=SimpleNamespace(db_path=db_path))


def _свод(имя="Просрочено"):
    свод = detectors.Свод()
    свод.отклонения = [detectors.Отклонение("прибор", имя, f"{имя}: беда")]
    return свод


def test_свод_подкладывается_к_промпту(tmp_path):
    текст = main.CoachBot._with_brief(
        _бот(tmp_path / "coach.db"), "промпт утра", _свод()
    )
    assert текст.startswith("промпт утра")
    assert "Просрочено: беда" in текст


def test_пустой_свод_оставляет_промпт_нетронутым(tmp_path):
    prompt = "промпт утра"
    assert main.CoachBot._with_brief(
        _бот(tmp_path / "coach.db"), prompt, detectors.Свод()
    ) == prompt
