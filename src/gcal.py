"""Инструменты Google Календаря для коуча — поверх Calendar API v3.

Событие ≠ задача: событие привязано к слоту времени (встреча, эфир, тренировка),
задача живёт в Todoist. Разграничение — в промпте коуча.

Выход в интернет к Google (oauth2.googleapis.com, www.googleapis.com) из РФ
блокируется, поэтому весь трафик идёт через Xray-мост (тот же прокси, что у
Whisper). Токен обновляется по refresh-flow: access_token живёт в памяти
процесса и переспрашивается, когда протух.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"


def build_calendar_server(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    calendar_id: str = "primary",
    tz: str = "Europe/Moscow",
    proxy: str | None = None,
):
    zone = ZoneInfo(tz)
    # access_token недолго живёт (час) — держим в памяти и обновляем по нужде.
    token_box: dict[str, Any] = {"access_token": None, "expiry": 0.0}

    async def access_token() -> str:
        if token_box["access_token"] and token_box["expiry"] - 60 > _time.time():
            return token_box["access_token"]
        async with httpx.AsyncClient(timeout=40, proxy=proxy) as http:
            response = await http.post(
                TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            response.raise_for_status()
            data = response.json()
        token_box["access_token"] = data["access_token"]
        token_box["expiry"] = _time.time() + int(data.get("expires_in", 3600))
        return token_box["access_token"]

    async def call(method: str, path: str, **kwargs) -> Any:
        token = await access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=40, proxy=proxy, headers=headers) as http:
            response = await http.request(method, f"{API}{path}", **kwargs)
            response.raise_for_status()
            if response.status_code == 204 or not response.content:
                return None
            return response.json()

    def ok(text: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": text}]}

    def now() -> datetime:
        return datetime.now(zone)

    # --- время: приём от модели и вывод человеку ---

    def to_google_time(value: str) -> dict[str, str]:
        """Строка от модели → поле start/end Google.

        «2026-07-29» (только дата) → событие на весь день.
        «2026-07-29T10:00» / «...T10:00:00» / «...+03:00» → привязка ко времени;
        без часового пояса подставляем рабочий (по умолчанию Москва).
        """
        value = value.strip()
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            return {"date": value}
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        return {"dateTime": dt.isoformat(), "timeZone": tz}

    def human_when(event: dict[str, Any]) -> str:
        start = event.get("start", {})
        end = event.get("end", {})
        if start.get("date"):
            return f"{start['date']} (весь день)"
        try:
            sd = datetime.fromisoformat(start["dateTime"]).astimezone(zone)
            out = sd.strftime("%d.%m %H:%M")
            if end.get("dateTime"):
                ed = datetime.fromisoformat(end["dateTime"]).astimezone(zone)
                out += "–" + ed.strftime("%H:%M")
            return out
        except (KeyError, ValueError):
            return start.get("dateTime", "?")

    def brief(event: dict[str, Any]) -> str:
        summary = event.get("summary") or "(без названия)"
        location = f" @ {event['location']}" if event.get("location") else ""
        return f"[{event['id']}] {human_when(event)} — {summary}{location}"

    def resolve_range(args: dict[str, Any]) -> tuple[str, str, str]:
        """Вернуть (timeMin, timeMax, подпись) из range или date_from/date_to."""
        preset = (args.get("range") or "").strip().lower()
        today = now().replace(hour=0, minute=0, second=0, microsecond=0)
        if preset in ("today", "сегодня"):
            return today.isoformat(), (today + timedelta(days=1)).isoformat(), "сегодня"
        if preset in ("tomorrow", "завтра"):
            start = today + timedelta(days=1)
            return start.isoformat(), (start + timedelta(days=1)).isoformat(), "завтра"
        if preset in ("week", "неделя", "7 days", ""):
            if not (args.get("date_from") or args.get("date_to")):
                return today.isoformat(), (today + timedelta(days=7)).isoformat(), "неделя"

        def edge(value: str | None, default: datetime) -> datetime:
            if not value:
                return default
            g = to_google_time(value)
            if "date" in g:
                return datetime.fromisoformat(g["date"]).replace(tzinfo=zone)
            return datetime.fromisoformat(g["dateTime"])

        start = edge(args.get("date_from"), today)
        end = edge(args.get("date_to"), start + timedelta(days=1))
        return start.isoformat(), end.isoformat(), f"{start.date()}…{end.date()}"

    # --- инструменты ---

    @tool(
        "list_events",
        "Показать события Google Календаря на период. range: today / tomorrow / week "
        "(по умолчанию неделя вперёд). Либо произвольный диапазон через date_from и "
        "date_to (ISO-дата «2026-07-29» или дата-время «2026-07-29T10:00»). "
        "Это способ узнать, что уже занято по времени — встречи, эфиры, тренировки.",
        {"range": str, "date_from": str, "date_to": str, "limit": int},
    )
    async def list_events(args: dict[str, Any]) -> dict[str, Any]:
        time_min, time_max, label = resolve_range(args)
        limit = min(int(args.get("limit") or 50), 250)
        data = await call(
            "GET",
            f"/calendars/{calendar_id}/events",
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": limit,
            },
        )
        items = (data or {}).get("items", [])
        if not items:
            return ok(f"На «{label}» событий в календаре нет.")
        return ok(f"События ({label}, {len(items)}):\n" + "\n".join(brief(e) for e in items))

    @tool(
        "create_event",
        "Создать событие в Google Календаре — то, что привязано ко времени (встреча, "
        "эфир, тренировка, блок «не трогать»). Для дел без жёсткого слота используй "
        "Todoist, не календарь. start и end — ISO-дата-время «2026-07-29T10:00» "
        "(рабочий часовой пояс подставится сам). Только дата «2026-07-29» — событие на "
        "весь день. Если end не задан — час от начала (или один день для «весь день»).",
        {
            "summary": str,
            "start": str,
            "end": str,
            "description": str,
            "location": str,
        },
    )
    async def create_event(args: dict[str, Any]) -> dict[str, Any]:
        start = to_google_time(args["start"])
        if args.get("end"):
            end = to_google_time(args["end"])
        elif "date" in start:
            nxt = datetime.fromisoformat(start["date"]) + timedelta(days=1)
            end = {"date": nxt.date().isoformat()}
        else:
            ed = datetime.fromisoformat(start["dateTime"]) + timedelta(hours=1)
            end = {"dateTime": ed.isoformat(), "timeZone": tz}
        payload: dict[str, Any] = {"summary": args["summary"], "start": start, "end": end}
        for key in ("description", "location"):
            if args.get(key):
                payload[key] = args[key]
        event = await call("POST", f"/calendars/{calendar_id}/events", json=payload)
        return ok(f"Создано событие {brief(event)}")

    @tool(
        "update_event",
        "Перенести или изменить событие (id берётся из list_events). Меняй только "
        "нужные поля: start/end — перенос времени, summary — название, description, "
        "location. Форматы времени те же, что у create_event.",
        {
            "event_id": str,
            "summary": str,
            "start": str,
            "end": str,
            "description": str,
            "location": str,
        },
    )
    async def update_event(args: dict[str, Any]) -> dict[str, Any]:
        event_id = str(args["event_id"])
        payload: dict[str, Any] = {}
        if args.get("start"):
            payload["start"] = to_google_time(args["start"])
        if args.get("end"):
            payload["end"] = to_google_time(args["end"])
        for key in ("summary", "description", "location"):
            if args.get(key):
                payload[key] = args[key]
        if not payload:
            return ok("Нечего менять — не передано ни одного поля.")
        event = await call("PATCH", f"/calendars/{calendar_id}/events/{event_id}", json=payload)
        return ok(f"Обновлено: {brief(event)}")

    @tool(
        "delete_event",
        "Удалить событие из Google Календаря по id (берётся из list_events). "
        "Отменённую встречу или ошибочный слот — убираем, а не оставляем висеть.",
        {"event_id": str},
    )
    async def delete_event(args: dict[str, Any]) -> dict[str, Any]:
        event_id = str(args["event_id"])
        await call("DELETE", f"/calendars/{calendar_id}/events/{event_id}")
        return ok(f"Событие {event_id} удалено.")

    return create_sdk_mcp_server(
        name="calendar",
        version="1.0.0",
        tools=[list_events, create_event, update_event, delete_event],
    )
