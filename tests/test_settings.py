"""Настройки коуча: модель и тумблеры кнопок.

Проверяется главное свойство файла, который читает код: **опечатка слышна**.
Молча проглоченная ошибка здесь означала бы бота, который думает не той
моделью или грузит кнопки, которые человек считал выключенными.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import settings


def write(brain: Path, body: str) -> Path:
    path = settings.path_in(brain)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Настройки\n\n```yaml\n{body}\n```\n", encoding="utf-8")
    return path


GOOD = """модель_разговора: "claude-opus-5"
модель_ночной_работы: "claude-fable-5"
кнопки_todoist:
  productivity_stats: off
кнопки_календаря: {}"""


def test_файла_нет_значит_умолчания(tmp_path: Path):
    values, problems = settings.read(tmp_path)
    assert problems == [] and values == settings.DEFAULTS


def test_годный_файл_читается(tmp_path: Path):
    write(tmp_path, GOOD)
    values, problems = settings.read(tmp_path)
    assert problems == []
    assert values["модель_разговора"] == "claude-opus-5"
    assert values["кнопки_todoist"] == {"productivity_stats": False}


def test_неизвестная_модель_ловится(tmp_path: Path):
    write(tmp_path, 'модель_разговора: "gpt-9"\nмодель_ночной_работы: "claude-fable-5"')
    values, problems = settings.read(tmp_path)
    assert any("gpt-9" in p for p in problems)
    assert values == settings.DEFAULTS  # умолчания целиком, а не половина файла


def test_тумблер_не_булев_ловится(tmp_path: Path):
    write(tmp_path, 'модель_разговора: "claude-fable-5"\n'
                    'модель_ночной_работы: "claude-fable-5"\n'
                    'кнопки_todoist:\n  add_task: "да"')
    _, problems = settings.read(tmp_path)
    assert any("add_task" in p and "on" in p for p in problems)


def test_опечатка_в_имени_кнопки_ловится(tmp_path: Path, monkeypatch):
    """Иначе человек думает, что выключил кнопку, а она грузится."""
    monkeypatch.setattr(settings, "known_button_names",
                        lambda: ({"add_task", "find_tasks"}, {"list_events"}))
    write(tmp_path, 'модель_разговора: "claude-fable-5"\n'
                    'модель_ночной_работы: "claude-fable-5"\n'
                    'кнопки_todoist:\n  add_taks: off')
    _, problems = settings.read(tmp_path)
    assert any("add_taks" in p for p in problems)


def test_правильное_имя_кнопки_проходит(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "known_button_names",
                        lambda: ({"add_task", "find_tasks"}, {"list_events"}))
    write(tmp_path, 'модель_разговора: "claude-fable-5"\n'
                    'модель_ночной_работы: "claude-fable-5"\n'
                    'кнопки_todoist:\n  add_task: off')
    values, problems = settings.read(tmp_path)
    assert problems == [] and values["кнопки_todoist"] == {"add_task": False}


def test_лишние_строки_ловятся(tmp_path: Path):
    write(tmp_path, 'модель_разговора: "claude-fable-5"\n'
                    'модель_ночной_работы: "claude-fable-5"\nцвет: синий')
    _, problems = settings.read(tmp_path)
    assert any("цвет" in p for p in problems)


def test_сломанный_yaml_ловится(tmp_path: Path):
    write(tmp_path, "модель_разговора: [не закрыт")
    values, problems = settings.read(tmp_path)
    assert problems and values == settings.DEFAULTS


def test_нет_блока_yaml_ловится(tmp_path: Path):
    path = settings.path_in(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Настройки\n\nмодель_разговора: claude-opus-5\n", encoding="utf-8")
    _, problems = settings.read(tmp_path)
    assert any("нет блока" in p for p in problems)


def test_разделы_кнопок_необязательны(tmp_path: Path):
    write(tmp_path, 'модель_разговора: "claude-opus-5"\nмодель_ночной_работы: "claude-fable-5"')
    values, problems = settings.read(tmp_path)
    assert problems == [] and values["кнопки_todoist"] == {}


# ── заведение файла ───────────────────────────────────────────────────────────

def test_файл_и_ссылка_заводятся_вместе(tmp_path: Path):
    """Заметка без ссылки — сирота, ссылка без заметки — битая."""
    index = tmp_path / "память" / "00-index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("## Где Василий сейчас\n\n- [[ритмы]] — во сколько коуч пишет\n"
                     "\n## Дальше\n", encoding="utf-8")

    assert settings.ensure(tmp_path, "2026-08-01") is True
    assert settings.path_in(tmp_path).exists()
    text = index.read_text(encoding="utf-8")
    assert "[[настройки]]" in text
    assert text.index("[[ритмы]]") < text.index("[[настройки]]") < text.index("## Дальше")


def test_повторное_заведение_ничего_не_дублирует(tmp_path: Path):
    index = tmp_path / "память" / "00-index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text("- [[ритмы]] — во сколько коуч пишет\n", encoding="utf-8")
    settings.ensure(tmp_path, "2026-08-01")
    settings.ensure(tmp_path, "2026-08-02")
    assert index.read_text(encoding="utf-8").count("[[настройки]]") == 1


def test_заготовка_читается_собственным_разбором(tmp_path: Path):
    """Файл, который код создал, код обязан суметь прочитать."""
    settings.ensure(tmp_path, "2026-08-01")
    values, problems = settings.read(tmp_path)
    assert problems == [] and values["модель_разговора"] in settings.KNOWN_MODELS


def test_описание_изменений_называет_что_поменялось():
    before = dict(settings.DEFAULTS)
    after = dict(settings.DEFAULTS, **{"модель_разговора": "claude-opus-5"})
    text = settings.describe(after, before)
    assert "claude-fable-5" in text and "claude-opus-5" in text


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
