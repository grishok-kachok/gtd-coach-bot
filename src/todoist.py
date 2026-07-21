"""Инструменты Todoist для коуча — поверх REST API v1.

Каждая закрытая или созданная ботом задача получает комментарий с авторством:
владелец должен видеть, кто это сделал — он сам или агент с его слов.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger(__name__)

API = "https://api.todoist.com/api/v1"
AUTHORSHIP = "закрыта Claude со слов Василия"
AUTHORSHIP_CREATED = "создана Claude со слов Василия"


def build_todoist_server(token: str):
    headers = {"Authorization": f"Bearer {token}"}

    async def call(method: str, path: str, **kwargs) -> Any:
        async with httpx.AsyncClient(timeout=30, headers=headers) as http:
            response = await http.request(method, f"{API}{path}", **kwargs)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    def ok(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    def brief(task: dict[str, Any]) -> str:
        due = (task.get("due") or {}).get("date") or "без даты"
        labels = " ".join(f"@{label}" for label in task.get("labels") or [])
        priority = task.get("priority") or 1  # 4 = p1 в UI
        flag = f" p{5 - priority}" if priority > 1 else ""
        return f"[{task['id']}] {task['content']} — {due}{flag} {labels}".rstrip()

    @tool(
        "list_tasks",
        "Показать задачи Todoist по фильтру на языке Todoist "
        "(например: today, overdue, 7 days, @актив, #Личное, no date & !@когда-нибудь). "
        "Это главный способ узнать, что у Василия в делах.",
        {"filter": str, "limit": int},
    )
    async def list_tasks(args: dict[str, Any]) -> dict[str, Any]:
        query = args.get("filter") or "today"
        limit = min(int(args.get("limit") or 30), 100)
        data = await call("GET", "/tasks/filter", params={"query": query, "limit": limit})
        tasks = data.get("results", [])
        if not tasks:
            return ok(f"По фильтру «{query}» задач нет.")
        return ok(f"Задачи по фильтру «{query}» ({len(tasks)}):\n" + "\n".join(brief(t) for t in tasks))

    @tool(
        "list_projects",
        "Список проектов и секций Todoist — чтобы класть задачу в правильное место.",
        {},
    )
    async def list_projects(args: dict[str, Any]) -> dict[str, Any]:
        projects = (await call("GET", "/projects", params={"limit": 100})).get("results", [])
        sections = (await call("GET", "/sections", params={"limit": 200})).get("results", [])
        by_project: dict[str, list[str]] = {}
        for section in sections:
            by_project.setdefault(section["project_id"], []).append(section["name"])
        lines = []
        for project in projects:
            names = by_project.get(project["id"], [])
            suffix = " → " + ", ".join(names) if names else ""
            lines.append(f"[{project['id']}] {project['name']}{suffix}")
        return ok("Проекты и секции:\n" + "\n".join(lines))

    @tool(
        "add_task",
        "Создать задачу в Todoist. Приоритет: 4 — самый высокий (p1 в приложении), 1 — обычный. "
        "due_string — человеческим языком по-русски: «завтра», «в пятницу», «каждый пн».",
        {
            "content": str,
            "project_id": str,
            "section_id": str,
            "due_string": str,
            "priority": int,
            "labels": str,
            "description": str,
            "duration_minutes": int,
        },
    )
    async def add_task(args: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": args["content"]}
        for key in ("project_id", "section_id", "description"):
            if args.get(key):
                payload[key] = args[key]
        if args.get("due_string"):
            payload["due_string"] = args["due_string"]
            payload["due_lang"] = "ru"
        if args.get("priority"):
            payload["priority"] = int(args["priority"])
        if args.get("labels"):
            payload["labels"] = [x.strip().lstrip("@") for x in str(args["labels"]).split(",") if x.strip()]
        if args.get("duration_minutes"):
            payload["duration"] = int(args["duration_minutes"])
            payload["duration_unit"] = "minute"

        task = await call("POST", "/tasks", json=payload)
        await call("POST", "/comments", json={"task_id": task["id"], "content": AUTHORSHIP_CREATED})
        return ok(f"Создана задача {brief(task)}")

    @tool(
        "complete_task",
        "Закрыть задачу в Todoist со слов Василия. Всегда оставляет пометку авторства.",
        {"task_id": str},
    )
    async def complete_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args["task_id"])
        await call("POST", "/comments", json={"task_id": task_id, "content": AUTHORSHIP})
        await call("POST", f"/tasks/{task_id}/close")
        return ok(f"Задача {task_id} закрыта, пометка авторства оставлена.")

    @tool(
        "update_task",
        "Изменить задачу: перенести срок (due_string), поменять приоритет, метки, текст. "
        "Для переноса дат у повторяющихся задач используй due_string.",
        {
            "task_id": str,
            "content": str,
            "due_string": str,
            "priority": int,
            "labels": str,
            "description": str,
        },
    )
    async def update_task(args: dict[str, Any]) -> dict[str, Any]:
        task_id = str(args["task_id"])
        payload: dict[str, Any] = {}
        for key in ("content", "description"):
            if args.get(key):
                payload[key] = args[key]
        if args.get("due_string"):
            payload["due_string"] = args["due_string"]
            payload["due_lang"] = "ru"
        if args.get("priority"):
            payload["priority"] = int(args["priority"])
        if args.get("labels"):
            payload["labels"] = [x.strip().lstrip("@") for x in str(args["labels"]).split(",") if x.strip()]
        if not payload:
            return ok("Нечего менять — не передано ни одного поля.")
        task = await call("POST", f"/tasks/{task_id}", json=payload)
        return ok(f"Обновлено: {brief(task)}")

    return create_sdk_mcp_server(
        name="todoist",
        version="1.0.0",
        tools=[list_tasks, list_projects, add_task, complete_task, update_task],
    )
