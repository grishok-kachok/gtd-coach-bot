"""Функции-«кнопки» Todoist — чистая логика поверх TodoistClient, без MCP.

Каждая функция принимает клиент и возвращает готовый человекочитаемый текст:
и MCP-сервер (мак), и бот (сервер) просто оборачивают этот текст. Так одно ядро
служит обоим каналам и будущим автоматизациям (ночной обход, поллер комментариев).

Приоритет — по конвенции API Todoist: 4 = самый высокий (p1 в приложении), 1 = обычный.
"""

from __future__ import annotations

from typing import Any

from .client import TodoistClient

# Пометки авторства — владелец должен видеть, кто тронул задачу: он сам или агент с его слов.
AUTHORSHIP_CLOSED = "закрыта Claude со слов Василия"
AUTHORSHIP_CREATED = "создана Claude со слов Василия"


# ── помощники ────────────────────────────────────────────────────────────────

def _parse_labels(labels: Any) -> list[str]:
    """Принять список или строку «актив, ждёт» → ['актив','ждёт'] (без @)."""
    if not labels:
        return []
    if isinstance(labels, str):
        parts = labels.split(",")
    else:
        parts = list(labels)
    return [str(x).strip().lstrip("@") for x in parts if str(x).strip()]


def brief(task: dict[str, Any]) -> str:
    """Однострочное описание задачи для человека."""
    due = (task.get("due") or {}).get("date") or "без даты"
    labels = " ".join(f"@{label}" for label in task.get("labels") or [])
    priority = task.get("priority") or 1  # 4 = p1 в UI
    flag = f" p{5 - priority}" if priority > 1 else ""
    return f"[{task['id']}] {task['content']} — {due}{flag} {labels}".rstrip()


def _task_payload(args: dict[str, Any]) -> dict[str, Any]:
    """Собрать тело задачи из общих полей (для add/update)."""
    payload: dict[str, Any] = {}
    for key in ("content", "description", "project_id", "section_id", "parent_id"):
        if args.get(key):
            payload[key] = args[key]
    if args.get("due_string"):
        payload["due_string"] = args["due_string"]
        payload["due_lang"] = "ru"
    if args.get("deadline_date"):
        payload["deadline_date"] = args["deadline_date"]  # YYYY-MM-DD
    if args.get("priority"):
        payload["priority"] = int(args["priority"])
    labels = _parse_labels(args.get("labels"))
    if labels:
        payload["labels"] = labels
    if args.get("duration"):
        payload["duration"] = int(args["duration"])
        payload["duration_unit"] = args.get("duration_unit") or "minute"
    return payload


# ── чтение ───────────────────────────────────────────────────────────────────

async def find_tasks(client: TodoistClient, filter: str = "today", limit: int = 30) -> str:
    """Задачи по фильтру Todoist (today, overdue, 7 days, @актив, #Личное, no date…)."""
    query = filter or "today"
    limit = min(int(limit or 30), 100)
    data = await client.get("/tasks/filter", params={"query": query, "limit": limit})
    tasks = data.get("results", []) if isinstance(data, dict) else data
    if not tasks:
        return f"По фильтру «{query}» задач нет."
    return f"Задачи по фильтру «{query}» ({len(tasks)}):\n" + "\n".join(brief(t) for t in tasks)


async def get_task(client: TodoistClient, task_id: str,
                   include_subtasks: bool = True, include_comments: bool = False) -> str:
    """Карточка задачи + при желании подзадачи и комментарии."""
    task = await client.get(f"/tasks/{task_id}")
    lines = [brief(task)]
    if task.get("description"):
        lines.append(f"  описание: {task['description']}")
    if include_subtasks:
        subs = await client.get_paginated("/tasks", params={"parent_id": task_id})
        if subs:
            lines.append(f"  подзадачи ({len(subs)}):")
            lines += [f"    • {brief(s)}" for s in subs]
    if include_comments:
        comments = await client.get_paginated("/comments", params={"task_id": task_id})
        if comments:
            lines.append(f"  комментарии ({len(comments)}):")
            lines += [f"    – {c.get('content', '')}" for c in comments]
    return "\n".join(lines)


async def get_comments(client: TodoistClient, task_id: str) -> str:
    """Прочитать комментарии задачи."""
    comments = await client.get_paginated("/comments", params={"task_id": task_id})
    if not comments:
        return f"У задачи {task_id} комментариев нет."
    return f"Комментарии задачи {task_id} ({len(comments)}):\n" + "\n".join(
        f"– {c.get('content', '')}" for c in comments
    )


async def get_structure(client: TodoistClient) -> str:
    """Карта: проекты + секции + метки — чтобы класть задачу в правильное место."""
    projects = await client.get_paginated("/projects", params={"limit": 100})
    sections = await client.get_paginated("/sections", params={"limit": 200})
    labels = await client.get_paginated("/labels", params={"limit": 200})
    by_project: dict[str, list[str]] = {}
    for section in sections:
        by_project.setdefault(section["project_id"], []).append(section["name"])
    lines = ["Проекты и секции:"]
    for project in projects:
        names = by_project.get(project["id"], [])
        suffix = " → " + ", ".join(names) if names else ""
        lines.append(f"  [{project['id']}] {project['name']}{suffix}")
    lines.append("Метки: " + ", ".join(f"@{label['name']}" for label in labels))
    return "\n".join(lines)


async def completed_history(client: TodoistClient, since: str, until: str,
                            project_id: str | None = None, limit: int = 50) -> str:
    """История завершённых задач за период (since/until — YYYY-MM-DD)."""
    params: dict[str, Any] = {
        "since": f"{since}T00:00:00",
        "until": f"{until}T23:59:59",
        "limit": min(int(limit or 50), 200),
    }
    if project_id:
        params["project_id"] = project_id
    data = await client.get("/tasks/completed/by_completion_date", params=params)
    items = data.get("items", data.get("results", [])) if isinstance(data, dict) else data
    if not items:
        return f"С {since} по {until} завершённых задач нет."
    lines = [f"Завершено с {since} по {until} ({len(items)}):"]
    for t in items:
        done = (t.get("completed_at") or "")[:10]
        lines.append(f"  ✓ {t.get('content', '')} — {done}")
    return "\n".join(lines)


# ── запись: задачи ─────────────────────────────────────────────────────────────

async def add_task(client: TodoistClient, content: str, *, authorship: bool = False,
                   **fields: Any) -> str:
    """Создать задачу или подзадачу (parent_id). due_string — словами по-русски."""
    payload = _task_payload({"content": content, **fields})
    task = await client.post("/tasks", json=payload)
    if authorship:
        await client.post("/comments", json={"task_id": task["id"], "content": AUTHORSHIP_CREATED})
    kind = "подзадача" if payload.get("parent_id") else "задача"
    return f"Создана {kind}: {brief(task)}"


async def add_tasks_bulk(client: TodoistClient, tasks: list[dict[str, Any]],
                         authorship: bool = False) -> str:
    """Создать пачку задач/подзадач. Каждый элемент — {content, ...поля}."""
    if not tasks:
        return "Пустой список — нечего создавать."
    created = []
    for item in tasks:
        content = item.get("content")
        if not content:
            continue
        payload = _task_payload(item)
        task = await client.post("/tasks", json=payload)
        if authorship:
            await client.post("/comments", json={"task_id": task["id"], "content": AUTHORSHIP_CREATED})
        created.append(task)
    return f"Создано задач: {len(created)}\n" + "\n".join(brief(t) for t in created)


async def update_task(client: TodoistClient, task_id: str, **fields: Any) -> str:
    """Изменить задачу: срок (due_string), приоритет, метки, текст, длительность."""
    payload = _task_payload(fields)
    payload.pop("parent_id", None)  # смену родителя делает move_task
    if not payload:
        return "Нечего менять — не передано ни одного поля."
    task = await client.post(f"/tasks/{task_id}", json=payload)
    return f"Обновлено: {brief(task)}"


async def complete_task(client: TodoistClient, task_id: str, reopen: bool = False,
                        authorship: bool = True) -> str:
    """Закрыть (или переоткрыть) задачу. По умолчанию оставляет пометку авторства."""
    task_id = str(task_id)
    if reopen:
        await client.post(f"/tasks/{task_id}/reopen")
        return f"Задача {task_id} снова открыта."
    if authorship:
        await client.post("/comments", json={"task_id": task_id, "content": AUTHORSHIP_CLOSED})
    await client.post(f"/tasks/{task_id}/close")
    return f"Задача {task_id} закрыта, пометка авторства оставлена."


async def move_task(client: TodoistClient, task_id: str, project_id: str | None = None,
                    section_id: str | None = None, parent_id: str | None = None) -> str:
    """Перенести задачу в проект/секцию или сделать её подзадачей (parent_id)."""
    body: dict[str, Any] = {}
    if project_id:
        body["project_id"] = project_id
    if section_id:
        body["section_id"] = section_id
    if parent_id:
        body["parent_id"] = parent_id
    if not body:
        return "Не указано, куда переносить (project_id / section_id / parent_id)."
    await client.post(f"/tasks/{task_id}/move", json=body)
    return f"Задача {task_id} перенесена."


async def delete_task(client: TodoistClient, task_id: str, confirm: bool = False) -> str:
    """Удалить задачу безвозвратно. Только при confirm=true (каскадом удалит подзадачи)."""
    if not confirm:
        return "Удаление не выполнено: нужен confirm=true. Удаление необратимо."
    await client.delete(f"/tasks/{task_id}")
    return f"Задача {task_id} удалена."


async def quick_add(client: TodoistClient, text: str) -> str:
    """Создать задачу одной строкой на естественном языке (#проект, @метка, p1, «завтра»)."""
    task = await client.post("/tasks/quick", json={"text": text})
    return f"Создано: {brief(task)}"


# ── запись: комментарии и структура ────────────────────────────────────────────

async def add_comment(client: TodoistClient, task_id: str, content: str) -> str:
    """Оставить комментарий к задаче."""
    comment = await client.post("/comments", json={"task_id": str(task_id), "content": content})
    return f"Комментарий добавлен к задаче {task_id} (id {comment.get('id')})."


async def manage_structure(client: TodoistClient, action: str, name: str,
                           project_id: str | None = None, parent_id: str | None = None) -> str:
    """Создать проект / секцию / метку. action: create_project | create_section | create_label."""
    if action == "create_project":
        body = {"name": name}
        if parent_id:
            body["parent_id"] = parent_id
        proj = await client.post("/projects", json=body)
        return f"Создан проект [{proj['id']}] {proj['name']}."
    if action == "create_section":
        if not project_id:
            return "Для секции нужен project_id."
        sec = await client.post("/sections", json={"name": name, "project_id": project_id})
        return f"Создана секция [{sec['id']}] {sec['name']}."
    if action == "create_label":
        label = await client.post("/labels", json={"name": name.lstrip("@")})
        return f"Создана метка @{label['name']}."
    return f"Неизвестное действие «{action}». Доступно: create_project, create_section, create_label."
