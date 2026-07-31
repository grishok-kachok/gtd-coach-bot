"""Промпты живут в плагине. Проверяем, что комплект на месте и читается.

Тест намеренно смотрит на НАСТОЯЩУЮ папку плагина в лаборатории, а не на
подсунутую временную: смысл переезда был в том, чтобы тексты лежали в плагине,
и тест обязан ломаться, если оттуда что-то пропало.
"""

from pathlib import Path

import pytest

from src import prompts

PLUGIN_PROMPTS = Path(__file__).resolve().parents[2] / "plugin" / "prompts"


@pytest.fixture(autouse=True)
def _real_plugin(monkeypatch):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", PLUGIN_PROMPTS)


def test_комплект_на_месте():
    assert prompts.missing() == []


@pytest.mark.parametrize("name", prompts.REQUIRED)
def test_каждый_промпт_читается_и_непустой(name):
    assert prompts.load(name).text


def test_роль_есть_у_ночных_промптов():
    # У чек-инов роли нет — они идут в общий разговор под конституцией.
    for name in ("выжимка-дня", "выжимка-укрупнение", "проверка-памяти", "подпись-коммита"):
        assert prompts.load(name).system, f"у «{name}» пропала роль"


def test_подстановки_на_месте():
    """Промпт без своей подстановки — молча испорченный промпт."""
    ожидания = {
        "выжимка-дня": ["{day}"],
        "выжимка-укрупнение": ["{period}", "{focus}"],
        "проверка-памяти": ["{day}", "{knowledge}", "{transcript}"],
        "подпись-коммита": ["{stat}", "{diff}", "{subjects}"],
        "месячный-итог": ["{month}"],
    }
    for name, поля in ожидания.items():
        текст = prompts.load(name).text
        for поле in поля:
            assert поле in текст, f"в «{name}» нет подстановки {поле}"


def test_дожимы_идут_по_порядку():
    подряд = prompts.followups()
    assert len(подряд) >= 2
    assert подряд[0].name == "дожим-1"
    assert подряд[1].name == "дожим-2"


def test_пропажа_видна_сразу(monkeypatch, tmp_path):
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    assert set(prompts.missing()) == set(prompts.REQUIRED)
    with pytest.raises(SystemExit):
        prompts.load("чекин-утро")


def test_шапка_не_попадает_в_текст(monkeypatch, tmp_path):
    (tmp_path / "проба.md").write_text(
        "---\nsystem: Роль такая-то\n---\n\nСам текст.\n", encoding="utf-8"
    )
    monkeypatch.setattr(prompts, "PROMPTS_DIR", tmp_path)
    п = prompts.load("проба")
    assert п.system == "Роль такая-то"
    assert п.text == "Сам текст."
