"""Инструменты Todoist для коуча — тонкие MCP-обёртки над ядром coach-todoist-mcp.

Вся логика (запросы к Todoist unified API v1, сборка человекочитаемого текста,
пометки авторства) живёт в `.coach_todoist_mcp.core`. Здесь только:
  открыть TodoistClient(token) → позвать функцию-«кнопку» → обернуть её str
  в MCP-ответ {"content":[{"type":"text","text":...}]}.

Так одно ядро служит и боту (этот файл), и MCP-серверу на ноутбуке. Ядро —
вендоренная копия (см. src/coach_todoist_mcp/__init__.py); на этапе 07 станет
обычной зависимостью.

Пометки авторства вшиты в ядро: закрытие задачи по умолчанию оставляет
комментарий «закрыта Claude со слов Василия»; создание метит по флагу authorship.
Egress к Todoist из РФ — через Xray-мост: TodoistClient(trust_env=True) сам
подхватывает HTTPS_PROXY из окружения контейнера (порт 1080).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from .coach_todoist_mcp import core
from .coach_todoist_mcp.client import TodoistClient, TodoistError

log = logging.getLogger(__name__)


def build_todoist_server(token: str):
    def ok(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    async def run(fn, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Открыть клиент, позвать функцию ядра, вернуть её текст. Ошибку —
        отдать словами, а не бросать: модель должна её увидеть и объяснить."""
        try:
            async with TodoistClient(token) as client:
                return ok(await fn(client, *args, **kwargs))
        except TodoistError as err:
            log.warning("Todoist: %s", err)
            return ok(f"Не получилось (Todoist): {err}")

    def collect(args: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
        """Собрать только переданные (непустые) поля — ядро само разберёт."""
        return {k: args[k] for k in keys if args.get(k) not in (None, "")}

    # ── чтение ──────────────────────────────────────────────────────────────

    @tool(
        "find_tasks",
        "Показать задачи Todoist по фильтру на языке Todoist "
        "(today, overdue, 7 days, @актив, #Личное, no date & !@когда-нибудь). "
        "Главный способ узнать, что у Василия в делах.",
        {"filter": str, "limit": int},
    )
    async def find_tasks(args: dict[str, Any]) -> dict[str, Any]:
        return await run(core.find_tasks, args.get("filter") or "today", int(args.get("limit") or 30))

    @tool(
        "get_task",
        "Карточка одной задачи по id: описание, при желании подзадачи и комментарии. "
        "Нужна, когда надо копнуть конкретное дело, а не список.",
        {"task_id": str, "include_subtasks": bool, "include_comments": bool},
    )
    async def get_task(args: dict[str, Any]) -> dict[str, Any]:
        return await run(
            core.get_task,
            str(args["task_id"]),
            include_subtasks=args.get("include_subtasks", True),
            include_comments=args.get("include_comments", False),
        )

    @tool(
        "get_comments",
        "Прочитать комментарии задачи по id (там же видны пометки авторства).",
        {"task_id": str},
    )
    async def get_comments(args: dict[str, Any]) -> dict[str, Any]:
        return await run(core.get_comments, str(args["task_id"]))

    @tool(
        "get_structure",
        "Карта Todoist: проекты, секции и метки — чтобы класть задачу в правильное место.",
        {},
    )
    async def get_structure(args: dict[str, Any]) -> dict[str, Any]:
        return await run(core.get_structure)

    @tool(
        "completed_history",
        "История завершённых задач за период (since и until — даты YYYY-MM-DD). "
        "Для обзора «что реально сделано» за неделю/месяц, при желании по одному проекту.",
        {"since": str, "until": str, "project_id": str, "limit": int},
    )
    async def completed_history(args: dict[str, Any]) -> dict[str, Any]:
        return await run(
            core.completed_history,
            args["since"],
            args["until"],
            project_id=args.get("project_id") or None,
            limit=int(args.get("limit") or 50),
        )

    # ── запись: задачи ──────────────────────────────────────────────────────

    _TASK_FIELDS = (
        "project_id", "section_id", "parent_id", "due_string",
        "deadline_date", "priority", "labels", "description",
    )

    @tool(
        "add_task",
        "Создать задачу (или подзадачу — через parent_id) в Todoist. Приоритет: "
        "4 — самый высокий (p1 в приложении), 1 — обычный. due_string — словами "
        "по-русски: «завтра», «в пятницу», «каждый пн». deadline_date — жёсткий "
        "дедлайн YYYY-MM-DD. labels — через запятую. duration_minutes — оценка времени. "
        "authorship=true пометит задачу «создана Claude со слов Василия».",
        {
            "content": str, "project_id": str, "section_id": str, "parent_id": str,
            "due_string": str, "deadline_date": str, "priority": int, "labels": str,
            "description": str, "duration_minutes": int, "authorship": bool,
        },
    )
    async def add_task(args: dict[str, Any]) -> dict[str, Any]:
        fields = collect(args, _TASK_FIELDS)
        if args.get("priority"):
            fields["priority"] = int(args["priority"])
        if args.get("duration_minutes"):
            fields["duration"] = int(args["duration_minutes"])
            fields["duration_unit"] = "minute"
        return await run(core.add_task, args["content"], authorship=args.get("authorship", False), **fields)

    @tool(
        "add_tasks_bulk",
        "Создать пачку задач за раз. tasks_json — JSON-массив объектов, каждый как "
        "у add_task: [{\"content\":\"...\",\"project_id\":\"...\",\"due_string\":\"завтра\"}, ...]. "
        "Удобно, когда крупное дело распадается на список подзадач.",
        {"tasks_json": str, "authorship": bool},
    )
    async def add_tasks_bulk(args: dict[str, Any]) -> dict[str, Any]:
        try:
            tasks = json.loads(args["tasks_json"])
        except (json.JSONDecodeError, TypeError) as err:
            return ok(f"tasks_json не разобран как JSON-массив: {err}")
        if not isinstance(tasks, list):
            return ok("tasks_json должен быть JSON-массивом объектов.")
        return await run(core.add_tasks_bulk, tasks, authorship=args.get("authorship", False))

    @tool(
        "update_task",
        "Изменить задачу: перенести срок (due_string), сменить приоритет, метки, "
        "текст, описание, дедлайн, длительность. Для переноса дат у повторяющихся "
        "задач используй due_string. Смену проекта/родителя делай через move_task.",
        {
            "task_id": str, "content": str, "due_string": str, "deadline_date": str,
            "priority": int, "labels": str, "description": str, "duration_minutes": int,
        },
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        fields = collect(args, ("content", "due_string", "deadline_date", "labels", "description"))
        if args.get("priority"):
            fields["priority"] = int(args["priority"])
        if args.get("duration_minutes"):
            fields["duration"] = int(args["duration_minutes"])
            fields["duration_unit"] = "minute"
        return await run(core.update_task, str(args["task_id"]), **fields)

    @tool(
        "complete_task",
        "Закрыть задачу в Todoist со слов Василия (по умолчанию оставляет пометку "
        "авторства). Чтобы, наоборот, вернуть ошибочно закрытую задачу — reopen=true.",
        {"task_id": str, "reopen": bool},
    )
    async def complete_task(args: dict[str, Any]) -> dict[str, Any]:
        return await run(core.complete_task, str(args["task_id"]), reopen=args.get("reopen", False))

    @tool(
        "move_task",
        "Перенести задачу в другой проект/секцию или сделать её подзадачей (parent_id). "
        "Именно для смены места — не для смены срока/текста (это update_task).",
        {"task_id": str, "project_id": str, "section_id": str, "parent_id": str},
    )
    async def move_task(args: dict[str, Any]) -> dict[str, Any]:
        return await run(
            core.move_task,
            str(args["task_id"]),
            project_id=args.get("project_id") or None,
            section_id=args.get("section_id") or None,
            parent_id=args.get("parent_id") or None,
        )

    @tool(
        "delete_task",
        "Удалить задачу безвозвратно (каскадом снесёт подзадачи). Необратимо, поэтому "
        "срабатывает только с confirm=true. Обычно закрывают (complete_task), а не удаляют.",
        {"task_id": str, "confirm": bool},
    )
    async def delete_task(args: dict[str, Any]) -> dict[str, Any]:
        return await run(core.delete_task, str(args["task_id"]), confirm=args.get("confirm", False))

    @tool(
        "quick_add",
        "Создать задачу одной строкой на естественном языке в стиле Todoist: "
        "«Позвонить Тане завтра в 15 #Личное @актив p1». Быстрый ввод, когда всё в одной фразе.",
        {"text": str},
    )
    async def quick_add(args: dict[str, Any]) -> dict[str, Any]:
        return await run(core.quick_add, args["text"])

    # ── комментарии и структура ─────────────────────────────────────────────

    @tool(
        "add_comment",
        "Оставить комментарий к задаче по id (заметка, контекст, договорённость).",
        {"task_id": str, "content": str},
    )
    async def add_comment(args: dict[str, Any]) -> dict[str, Any]:
        return await run(core.add_comment, str(args["task_id"]), args["content"])

    @tool(
        "manage_structure",
        "Создать элемент структуры Todoist. action: create_project | create_section | "
        "create_label. Для секции нужен project_id; для вложенного проекта — parent_id. "
        "Пользуйся редко и осознанно — структуру задаёт Василий.",
        {"action": str, "name": str, "project_id": str, "parent_id": str},
    )
    async def manage_structure(args: dict[str, Any]) -> dict[str, Any]:
        return await run(
            core.manage_structure,
            args["action"],
            args["name"],
            project_id=args.get("project_id") or None,
            parent_id=args.get("parent_id") or None,
        )

    return create_sdk_mcp_server(
        name="todoist",
        version="2.0.0",
        tools=[
            find_tasks, get_task, get_comments, get_structure, completed_history,
            add_task, add_tasks_bulk, update_task, complete_task, move_task,
            delete_task, quick_add, add_comment, manage_structure,
        ],
    )
