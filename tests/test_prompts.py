"""Промпты живут в плагине. Проверяем, что комплект на месте и читается.

Тест намеренно смотрит на НАСТОЯЩУЮ папку плагина в соседнем репозитории, а не на
подсунутую временную: смысл переезда был в том, чтобы тексты лежали в плагине,
и тест обязан ломаться, если оттуда что-то пропало.

Плагина рядом нет — тесты пропускаются с объяснением, а не падают: склонировать
один только бот законно, и человек не должен получать за это красный прогон.
"""

import pytest

from src import prompts

from conftest import ПЛАГИН, ПЛАГИН_ИМЕНА

# Имя папки плагина задаёт тот, кто клонировал: у ученика это `gtd-coach`,
# у автора исторически `plugin`. Константа здесь стоила шестнадцати падений
# на ровном месте — теперь ищем по списку имён (conftest), а не по одному.
pytestmark = pytest.mark.skipif(
    ПЛАГИН is None,
    reason="плагина рядом нет — искал папки " + ", ".join(ПЛАГИН_ИМЕНА)
    + ". Склонируй gtd-coach соседней папкой, и тесты промптов оживут.",
)

PLUGIN_PROMPTS = (ПЛАГИН / "prompts") if ПЛАГИН else None


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
        "напоминание-об-обзоре": ["{повод}", "{что}"],
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
