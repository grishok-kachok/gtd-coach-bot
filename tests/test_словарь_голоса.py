"""Подсказка распознавателю собирается из трёх источников, а не ведётся руками.

Проверяется главное свойство: слово, описанное в глоссарии памяти, доезжает
до распознавателя само. Раньше его приходилось дублировать в настройках —
и оно там не появлялось, потому что человек про второй список не знал.

Слова в фикстурах намеренно выдуманные: репозиторий публичный, личного тут
быть не должно, а проверяется механика, а не чей-то словарь.
"""

from __future__ import annotations

from pathlib import Path

from src import glossary, voice

ШАПКА = """---
title: глоссарий
type: standard
schema_version: "1.0"
status: stable
created: 2026-08-03
{строка}---

# Глоссарий
"""


def написать(brain: Path, строка: str = "") -> Path:
    path = glossary.path_in(brain)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ШАПКА.format(строка=строка), encoding="utf-8")
    glossary.забыть()
    return path


# ── чтение шапки ──────────────────────────────────────────────────────────────

def test_слова_из_шапки_читаются(tmp_path: Path):
    написать(tmp_path, "voice: [Ктототам, Зимогорск]\n")
    assert glossary.слова(tmp_path) == ["Ктототам", "Зимогорск"]


def test_нет_файла_нет_слов(tmp_path: Path):
    glossary.забыть()
    assert glossary.слова(tmp_path) == []


def test_нет_ключа_нет_слов(tmp_path: Path):
    написать(tmp_path)
    assert glossary.слова(tmp_path) == []


def test_сломанная_шапка_не_роняет(tmp_path: Path):
    """Подсказка — улучшение, а не условие работы: голосовые разбираться не перестают."""
    path = glossary.path_in(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: [не закрыт\n---\n\n# Глоссарий\n", encoding="utf-8")
    glossary.забыть()
    assert glossary.слова(tmp_path) == []


def test_строка_вместо_списка_не_роняет(tmp_path: Path):
    написать(tmp_path, "voice: Ктототам, Зимогорск\n")
    assert glossary.слова(tmp_path) == []


def test_правка_файла_подхватывается_без_перезапуска(tmp_path: Path):
    """Новое слово должно работать сразу — иначе его снова будут вести вторым списком."""
    написать(tmp_path, "voice: [Ктототам]\n")
    assert glossary.слова(tmp_path) == ["Ктототам"]

    path = glossary.path_in(tmp_path)
    текст = path.read_text(encoding="utf-8").replace(
        "voice: [Ктототам]", "voice: [Ктототам, Зимогорск]")
    path.write_text(текст, encoding="utf-8")
    # Кэш не сбрасываем руками: он обязан заметить правку сам, по файлу.
    assert glossary.слова(tmp_path) == ["Ктототам", "Зимогорск"]


def test_нетронутый_файл_второй_раз_не_читается(tmp_path: Path, monkeypatch):
    написать(tmp_path, "voice: [Ктототам]\n")
    assert glossary.слова(tmp_path) == ["Ктототам"]

    def не_звать(path):
        raise AssertionError("нетронутый файл читать заново незачем")

    monkeypatch.setattr(glossary, "_разобрать", не_звать)
    assert glossary.слова(tmp_path) == ["Ктототам"]


# ── сборка подсказки ─────────────────────────────────────────────────────────

def test_слова_глоссария_попадают_в_подсказку():
    текст = voice.подсказка(["Ктототам", "зимогорск.рф"])
    assert "Ктототам" in текст and "зимогорск.рф" in текст


def test_общие_слова_и_свои_склеиваются():
    текст = voice.подсказка(["Ктототам"])
    assert "Todoist" in текст and "Ктототам" in текст


def test_дубли_убираются_без_учёта_регистра():
    слова = voice.собрать_словарь(["Todoist", "Ктототам", "ктототам"])
    assert слова.count("Ктототам") == 1
    assert "ктототам" not in слова          # пишется первое написание
    assert слова.count("Todoist") == 1


def test_пустой_глоссарий_ничего_не_меняет():
    """Пункт «не ломать текущее поведение»: подсказка ровно та же, что была."""
    было = voice.ОБЩЕЕ + voice.БАЗОВЫЙ_СЛОВАРЬ + "."
    assert voice.подсказка() == было
    assert voice.подсказка([]) == было
    assert voice.HINT == было


def test_подсказка_не_длиннее_потолка():
    длинный = [f"Слово{n}" for n in range(200)]
    текст = voice.подсказка(длинный)
    assert len(текст) <= voice.ПОТОЛОК_СИМВОЛОВ


def test_при_обрезке_общие_слова_остаются():
    """Старшинство: общие слова нужны любому разговору, свои — по порядку списка."""
    текст = voice.подсказка([f"Слово{n}" for n in range(200)])
    assert "Todoist" in текст and "эфир" in текст
    assert "Слово0" in текст           # начало своего списка тоже доезжает
    assert "Слово199" not in текст     # хвост длинного списка отрезан


def test_очень_длинное_первое_слово_не_роняет():
    """Одно слово длиннее потолка — подсказка всё равно строка, а не исключение."""
    текст = voice.подсказка(["Я" * 500])
    assert текст.startswith(voice.ОБЩЕЕ) and текст.endswith(".")


# ── проводка: от файла в памяти до расшифровки ───────────────────────────────

def test_бот_берёт_слова_из_глоссария(tmp_path: Path):
    """Та самая проводка, обрыв которой и был бедой заявки."""
    from types import SimpleNamespace

    from src.main import CoachBot

    написать(tmp_path, "voice: [Ктототам]\n")
    бот = SimpleNamespace(engine=SimpleNamespace(brain_dir=tmp_path))

    слова = CoachBot._слова_для_распознавателя(бот)
    assert слова == ["Ктототам"]
    assert "Ктототам" in voice.подсказка(слова)


def test_глоссарий_без_строки_не_роняет_расшифровку(tmp_path: Path):
    """Строки `voice` нет — подсказка та же, что была до заявки."""
    from types import SimpleNamespace

    from src.main import CoachBot

    написать(tmp_path)
    бот = SimpleNamespace(engine=SimpleNamespace(brain_dir=tmp_path))

    assert CoachBot._слова_для_распознавателя(бот) == []
    assert voice.подсказка([]) == voice.HINT
