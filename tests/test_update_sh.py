"""Команда обновления и то, что смонтировано, обязаны говорить об одном.

`update.sh` тянет клоны по своим путям, а контейнер читает их по путям
из `docker-compose.yml`. Разъедутся — обновится не то, что работает, и увидеть
это будет неоткуда: команда отрапортует успех, бот продолжит крутить старое.
Тот же класс, что «кнопка без повода»: поломки не видно.

Проверяем связку целиком, а не по файлу: путь из update.sh → том в compose,
и цепочку версии сборки Dockerfile → compose → update.sh.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

БОТ = Path(__file__).resolve().parents[1]
UPDATE = БОТ / "update.sh"
COMPOSE = БОТ / "docker-compose.yml"
DOCKERFILE = БОТ / "Dockerfile"


@pytest.fixture(scope="module")
def тексты() -> dict[str, str]:
    нет = [f.name for f in (UPDATE, COMPOSE, DOCKERFILE) if not f.exists()]
    if нет:
        pytest.skip(f"нет файлов: {', '.join(нет)}")
    return {ф.name: ф.read_text() for ф in (UPDATE, COMPOSE, DOCKERFILE)}


def умолчание(текст: str, имя: str) -> str:
    """Путь по умолчанию из строки вида `PLUGIN="${PLUGIN_DIR:-/opt/apps/…}"`."""
    найдено = re.search(rf'{имя}:-([^}}"]+)', текст)
    assert найдено, f"в update.sh нет умолчания для {имя}"
    return найдено.group(1).strip()


@pytest.mark.parametrize("переменная", ["PLUGIN_DIR", "TODOIST_DIR", "GCAL_DIR"])
def test_путь_из_команды_обновления_смонтирован_в_контейнер(тексты, переменная):
    путь = умолчание(тексты["update.sh"], переменная)
    assert f"- {путь}" in тексты["docker-compose.yml"], (
        f"{переменная} = {путь}, а такого тома в docker-compose.yml нет: "
        f"обновится не то, что работает"
    )


def test_имя_контейнера_у_команды_и_у_compose_одно(тексты):
    """По этому имени команда ждёт `healthy` и снимает логи. Разъедется —

    обновление будет молча ждать несуществующий контейнер две с половиной минуты."""
    имя = умолчание(тексты["update.sh"], "CONTAINER_NAME")
    assert f"container_name: {имя}" in тексты["docker-compose.yml"]


def test_версия_сборки_доезжает_до_образа(тексты):
    """Цепочка из трёх звеньев: команда считает sha → compose передаёт аргумент

    → Dockerfile кладёт его в переменную окружения. Порвётся любое — бот
    перестанет знать свою версию, и `/version` будет честно врать «неизвестна»,
    не объясняя почему."""
    assert "GIT_SHA=$(git -C" in тексты["update.sh"], "команда не считает sha"
    assert "export GIT_SHA" in тексты["update.sh"], "sha не уедет в docker compose"
    assert "GIT_SHA: ${GIT_SHA:-}" in тексты["docker-compose.yml"], "compose не передаёт"
    assert "ARG GIT_SHA" in тексты["Dockerfile"], "образ не принимает"
    assert "GIT_SHA=${GIT_SHA}" in тексты["Dockerfile"], "образ не запоминает"


def test_движок_объявлен_и_запинен_в_образе(тексты):
    """`/version` спрашивает у образа, что за движок в нём стоит. Если ENV нет,

    бот не знает даже своей текущей версии — сравнивать не с чем."""
    assert re.search(r"ARG CLAUDE_CODE_VERSION=\d+\.\d+", тексты["Dockerfile"]), (
        "версия движка обязана быть запинена: непиненная пересборка 30.07.2026 "
        "молча привезла другой мозг"
    )
    assert "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}" in тексты["Dockerfile"]


def test_команда_обновления_запускается_а_не_только_лежит(тексты):
    assert UPDATE.stat().st_mode & 0o111, "update.sh без права на запуск"
    assert тексты["update.sh"].startswith("#!/bin/bash")


def test_несклонированную_часть_команда_пропускает(тексты):
    """Ученик без календаря обязан обновляться той же командой. Проверяем

    намерение в коде; что оно работает — проверено живым прогоном (задача 13)."""
    assert 'if [ ! -d "$dir/.git" ]' in тексты["update.sh"]
    # Слово, а не фраза целиком: формат вывода правился и будет правиться,
    # а сторож обязан стеречь поведение, а не расстановку тире.
    assert "не установлен" in тексты["update.sh"]
    assert "пропускаю" in тексты["update.sh"]
