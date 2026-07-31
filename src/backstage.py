"""Фоновая находка, которой нужен человек, ставит задачу в Todoist с датой.

Правило владельца 31.07: **не сообщение в телеграм**. Сообщение уползает вверх
и забывается; задача с датой возвращается сама — это и есть «помнит система,
а не голова».

Куплено находкой: три копилки (заявки, предложения памяти, промахи памяти)
заполнялись молча, и ни один ритуал не говорил «посмотри копилки». Копилка,
в которую не заглядывают, — свалка.

Две тонкости, иначе Todoist засорится:

1. **Одна задача на копилку, а не на каждую находку.** Не «три промаха — три
   задачи», а одна, и в ней комментарием, сколько накопилось.
2. **Задача уже стоит и не закрыта — второй не создаём**, дописываем комментарий.

Один дом на всех потребителей: ночная проверка памяти, заявки, сторож потолка
загрузки, сломанные ритмы, упавшая синхронизация мозга. Шесть копий этой логики
были бы шестью местами, где её потом чинить.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from coach_todoist_mcp.client import TodoistClient, TodoistError

log = logging.getLogger(__name__)

# Куда кладём. Это дела про самого коуча и его хозяйство, а по правилу от 24.07
# личные ИИ-агенты и серверная инфраструктура — обычная работа, проект «Рабочее».
PROJECT = os.environ.get("BACKSTAGE_PROJECT", "Рабочее")


@dataclass(frozen=True)
class Finding:
    """Что фоновая работа нашла и какой задачей это дёргает за рукав."""

    title: str        # ровно эта строка ищется в Todoist — она и есть опознавание
    why: str          # уходит в описание карточки при заведении
    due: str = "сегодня"


# Полный список фоновых источников. Список, а не строки по месту вызова: иначе
# завтра появится седьмой, и никто не вспомнит про правило «одна задача на копилку».
FINDINGS = {
    "предложения": Finding(
        title="Разобрать предложения памяти",
        why="Ночная проверка предложила записать что-то про Василия. "
            "Файл: память/журнал/предложения-памяти.md. Разобранное вычеркнуть.",
    ),
    "промахи": Finding(
        title="Посмотреть промахи памяти",
        why="Ночь заметила, где память подвела: коуч полез в архив или переспросил "
            "за тем, что должно было лежать в памяти. Файл: память/журнал/промахи-памяти.md. "
            "Это улики для мастерской, а не рабочий файл.",
    ),
    "заявка": Finding(
        title="Разобрать заявку к коучу",
        why="Василий попросил способность, которой у коуча нет. Логику меняет человек "
            "в мастерской. Файл: память/журнал/заявки.md.",
    ),
    "потолок": Finding(
        title="Память пробила потолок стартовой загрузки",
        why="Сумма загрузки разошлась с паспортом (config.yaml мозга, startup.budget). "
            "Количество кусков памяти постоянно — значит выжимки стали многословнее. "
            "Чинить надо промпт выжимки, а не резать окно.",
    ),
    "ритмы": Finding(
        title="Расписание коуча не читается — живу по прежнему",
        why="Файл память/состояние/ритмы.md сломан, бот взял умолчания. "
            "Пока не починен, чек-ины приходят не тогда, когда просили.",
    ),
    "синхронизация": Finding(
        title="Мозг не уехал на GitHub",
        why="git push из контейнера не прошёл. Пока не починено, записанное коучем "
            "живёт в одном экземпляре на сервере и в бэкап уедет, а на GitHub — нет.",
    ),
}


async def _open_task(client: TodoistClient, title: str) -> dict | None:
    """Незакрытая задача ровно с таким названием, если она уже стоит.

    Ищем поиском по тексту, а не по метке: метка — это ещё одна сущность,
    за которой пришлось бы следить, а название задачи мы задаём сами и оно
    стабильно. Совпадение проверяем точное: `search:` находит и по кусочку.
    """
    query = f"search: {title}"
    try:
        data = await client.get("/tasks/filter", params={"query": query, "limit": 50})
    except TodoistError as err:
        log.warning("не смог поискать задачу «%s»: %s", title, err)
        raise
    tasks = data.get("results", []) if isinstance(data, dict) else (data or [])
    for task in tasks:
        if (task.get("content") or "").strip() == title:
            return task
    return None


async def _project_id(client: TodoistClient, name: str) -> str | None:
    try:
        data = await client.get("/projects", params={"limit": 100})
    except TodoistError as err:
        log.warning("не смог прочитать проекты: %s", err)
        return None
    projects = data.get("results", []) if isinstance(data, dict) else (data or [])
    for project in projects:
        if (project.get("name") or "").strip() == name:
            return project.get("id")
    log.warning("проект «%s» не найден — кладу задачу в Inbox", name)
    return None


async def raise_task(token: str, kind: str, note: str) -> str:
    """Дёрнуть за рукав задачей. Возвращает «создана», «дополнена» или «».

    Пустая строка — значит до Todoist не доехали. Молча глотать нельзя, но
    и ронять ночной прогон из-за недоступного Todoist тоже нельзя: находка
    уже лежит в копилке, задача — второй контур, а не единственный.
    """
    finding = FINDINGS.get(kind)
    if finding is None:
        raise KeyError(f"неизвестный источник фоновой находки: {kind}")

    try:
        async with TodoistClient(token) as client:
            standing = await _open_task(client, finding.title)
            if standing:
                await client.post(
                    "/comments", json={"task_id": standing["id"], "content": note}
                )
                log.info("фоновая находка «%s»: задача уже стоит, дописал комментарий", kind)
                return "дополнена"

            payload = {
                "content": finding.title,
                "description": f"{finding.why}\n\n{note}",
                "due_string": finding.due,
                "due_lang": "ru",
                "labels": ["актив"],
            }
            project = await _project_id(client, PROJECT)
            if project:
                payload["project_id"] = project
            await client.post("/tasks", json=payload)
            log.info("фоновая находка «%s»: завёл задачу «%s»", kind, finding.title)
            return "создана"
    except TodoistError as err:
        log.error("фоновая находка «%s» не доехала до Todoist: %s", kind, err)
        return ""
    except OSError as err:  # сеть отвалилась — тот же исход, ночной прогон не роняем
        log.error("фоновая находка «%s»: сеть недоступна: %s", kind, err)
        return ""
