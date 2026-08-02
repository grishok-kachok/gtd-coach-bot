"""Версии частей продукта: что стоит и что новее.

Проверяем на НАСТОЯЩИХ репозиториях, а не на подделанном выводе git: разбор
чужого текста ловит опечатку в регулярке, но не ловит того, что команда
в контейнере вообще не выполняется (чужой владелец папки — `dubious ownership`,
поймано 02.08.2026). Сеть подменяем — она единственное, чего в тестах нет.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from src import versions


def репозиторий(где, origin: str = "git@github.com:vefmvai/todoist-mcp.git") -> str:
    """Настоящий git-репозиторий с одним коммитом. Возвращает его sha."""
    где.mkdir(parents=True, exist_ok=True)
    выполнить = lambda *арг: subprocess.run(  # noqa: E731
        ["git", *арг], cwd=где, check=True, capture_output=True, text=True
    )
    выполнить("init", "-q", "-b", "main")
    выполнить("config", "user.email", "test@example.com")
    выполнить("config", "user.name", "test")
    (где / "файл.txt").write_text("раз")
    выполнить("add", ".")
    выполнить("commit", "-qm", "первый")
    выполнить("remote", "add", "origin", origin)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=где, capture_output=True, text=True
    ).stdout.strip()


# ── координаты репозитория ────────────────────────────────────────────────────

@pytest.mark.parametrize("адрес,ждём", [
    ("git@github.com:vefmvai/todoist-mcp.git", "vefmvai/todoist-mcp"),
    ("https://github.com/vefmvai/gtd-coach.git", "vefmvai/gtd-coach"),
    ("https://github.com/vefmvai/gcal-mcp", "vefmvai/gcal-mcp"),
    ("https://github.com/ученик/gtd-coach-bot/", "ученик/gtd-coach-bot"),
    ("", ""),
    ("git@gitlab.com:кто-то/что-то.git", ""),
])
def test_координаты_из_адреса(адрес, ждём):
    assert versions.координаты(адрес) == ждём


def test_форк_ученика_спрашивается_у_его_репозитория():
    """Координаты берутся из origin, а не зашиты: иначе форк спрашивал бы

    про свежесть у чужого репозитория и всегда считал себя отставшим."""
    assert versions.координаты("git@github.com:ученик/todoist-mcp.git") == "ученик/todoist-mcp"


# ── версии движка ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("есть,вышла,новее", [
    ("2.1.216", "2.2.0", True),
    ("2.1.216", "2.1.216", False),
    ("2.1.216", "2.1.215", False),
    # Строкой такие версии сравнивать нельзя: «2.9.9» больше «2.10.0» по алфавиту.
    ("2.9.9", "2.10.0", True),
    ("2.10.0", "2.9.9", False),
    ("", "2.2.0", False),
    ("2.1.216", "", False),
])
def test_сравнение_версий_числами_а_не_строкой(есть, вышла, новее):
    assert versions.версия_новее(есть, вышла) is новее


# ── что стоит ─────────────────────────────────────────────────────────────────

def test_версия_смонтированного_клона_читается(tmp_path):
    sha = репозиторий(tmp_path / "плагин")
    часть = versions.Часть("плагин", tmp_path / "плагин" / ".git")
    assert versions.местное(часть).стоит == sha[:7]


def test_несклонированный_сосед_это_не_установлен_а_не_поломка(tmp_path):
    """Ученик без календаря: папки нет, и это нормальное состояние."""
    часть = versions.Часть("gcal-mcp", tmp_path / "нет-такого.git")
    состояние = versions.местное(часть)
    assert состояние.заметка == "не установлен"
    assert состояние.стоит == ""


def test_версия_бота_берётся_из_образа_а_не_из_клона(tmp_path, monkeypatch):
    """Клон и образ — разные вещи, и путать их дорого: 31.07.2026 выкатка

    отрапортовала успех при живом контейнере со старым кодом."""
    sha = репозиторий(tmp_path / "бот")
    monkeypatch.setenv("GIT_SHA", "0000000abcdef")
    часть = versions.Часть("бот", tmp_path / "бот" / ".git", из_образа=True)
    состояние = versions.местное(часть)
    assert состояние.стоит == "0000000"
    assert состояние.лежит == sha[:7], "клон впереди образа — это надо показать"


def test_клон_совпал_с_образом_и_про_пересборку_молчим(tmp_path, monkeypatch):
    sha = репозиторий(tmp_path / "бот")
    monkeypatch.setenv("GIT_SHA", sha)
    часть = versions.Часть("бот", tmp_path / "бот" / ".git", из_образа=True)
    assert versions.местное(часть).лежит == ""


def test_сборка_руками_честно_называется_неизвестной(tmp_path, monkeypatch):
    репозиторий(tmp_path / "бот")
    monkeypatch.delenv("GIT_SHA", raising=False)
    часть = versions.Часть("бот", tmp_path / "бот" / ".git", из_образа=True)
    assert versions.местное(часть).заметка == "сборка неизвестна"


def test_ветка_и_координаты_берутся_у_клона(tmp_path):
    репозиторий(tmp_path / "мцп")
    коорд, ветка = versions.адрес(versions.Часть("todoist-mcp", tmp_path / "мцп" / ".git"))
    assert (коорд, ветка) == ("vefmvai/todoist-mcp", "main")


# ── что новее ─────────────────────────────────────────────────────────────────

@pytest.fixture
def четыре(tmp_path, monkeypatch):
    """Четыре настоящих клона на своих местах, как в контейнере.

    Отдаёт «что где стоит» — координаты GitHub → короткий sha. Подделка сети
    отвечает из этого же словаря: свежий ответ обязан совпадать с местным,
    иначе «всё свежее» в тесте недостижимо и проверять нечего.
    """
    свои: dict[str, str] = {}
    for имя in ("gtd-coach-bot", "todoist-mcp", "gcal-mcp"):
        sha = репозиторий(tmp_path / "repos" / имя, f"git@github.com:vefmvai/{имя}.git")
        # `.git` смонтирована отдельно от рабочего дерева — ровно как в контейнере.
        (tmp_path / "repos" / имя / ".git").rename(tmp_path / "repos" / f"{имя}.git")
        свои[f"vefmvai/{имя}"] = sha[:7]
    свои["vefmvai/gtd-coach"] = репозиторий(
        tmp_path / "plugin", "git@github.com:vefmvai/gtd-coach.git")[:7]
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "repos"))
    monkeypatch.setenv("PLUGIN_DIR", str(tmp_path / "plugin"))
    # Образ собран из того же коммита, что лежит в клоне: «подтянуто, но не
    # собрано» — отдельный случай, у него свой тест.
    monkeypatch.setenv("GIT_SHA", свои["vefmvai/gtd-coach-bot"])
    monkeypatch.setenv("CLAUDE_CODE_VERSION", "2.1.216")
    return свои


def test_сеть_молчит_и_мы_говорим_что_не_проверили(четыре, monkeypatch):
    """Сторож, который не смог проверить, обязан сказать «не проверил».

    Молчаливое «всё свежее» при мёртвой сети — худший из ответов."""
    async def падает(*_а, **_к):
        raise RuntimeError("сети нет")

    monkeypatch.setattr(versions, "_свежий_sha", падает)
    monkeypatch.setattr(versions, "_свежий_движок", падает)
    отчёт = asyncio.run(versions.проверить(таймаут=1))
    assert len(отчёт) == 5
    assert all(not с.устарело for с in отчёт), "не проверил — не значит устарел"
    assert all("не проверил" in с.заметка for с in отчёт)


def test_расхождение_видно_и_разложено_по_двум_поводам(четыре, monkeypatch):
    async def свежий(_клиент, коорд, _ветка):
        # Новое вышло только у одного инструмента, остальные части свежие.
        return "bbbbbbb" if коорд.endswith("todoist-mcp") else четыре[коорд]

    async def отставание(*_а, **_к):
        return 3

    async def движок(_клиент):
        return "2.2.0"

    monkeypatch.setattr(versions, "_свежий_sha", свежий)
    monkeypatch.setattr(versions, "_отставание", отставание)
    monkeypatch.setattr(versions, "_свежий_движок", движок)
    отчёт = asyncio.run(versions.проверить(таймаут=1))
    репозитории, движок_состояние = versions.устаревшие(отчёт)
    assert [с.имя for с in репозитории] == ["todoist-mcp"]
    assert репозитории[0].отстал_на == 3
    assert движок_состояние is not None and движок_состояние.новее == "2.2.0"


def test_движок_отдельным_поводом_даже_когда_репозитории_свежие(четыре, monkeypatch):
    """Действия разные: репозитории тянет команда, движок поднимает человек."""
    async def свежий(_клиент, коорд, _ветка):
        return четыре[коорд]

    async def движок(_клиент):
        return "2.5.0"

    monkeypatch.setattr(versions, "_свежий_sha", свежий)
    monkeypatch.setattr(versions, "_свежий_движок", движок)
    отчёт = asyncio.run(versions.проверить(таймаут=1))
    репозитории, движок_состояние = versions.устаревшие(отчёт)
    assert репозитории == []
    assert движок_состояние is not None


# ── как это читается ──────────────────────────────────────────────────────────

def test_отчёт_начинается_с_шапки_продукта():
    """Без шапки это читается как пять хозяйств, а вещь одна."""
    текст = versions.отчёт_текстом([versions.Состояние(имя="бот", стоит="837181b")])
    assert текст.startswith("GTD-коуч\n")
    assert "Всё свежее" in текст


def test_в_отчёте_видно_и_что_стоит_и_что_новее():
    текст = versions.отчёт_текстом([
        versions.Состояние(имя="плагин", стоит="ff704ba", новее="1234567", отстал_на=3),
        versions.Состояние(имя="движок", стоит="2.1.216", новее="2.2.0"),
    ])
    assert "ff704ba" in текст and "1234567" in текст and "+3" in текст
    assert "./update.sh" in текст, "сказали про обновление — скажи и как обновиться"
    assert "вручную" in текст, "движок запинен намеренно, его команда не трогает"


def test_несобранный_клон_видно_отдельной_строкой():
    текст = versions.отчёт_текстом([
        versions.Состояние(имя="бот", стоит="aaaaaaa", лежит="bbbbbbb"),
    ])
    assert "не собран" in текст
