"""Тонкий async-клиент к Todoist unified API v1.

Только HTTP-слой: заголовок с токеном, базовый URL, разбор ответа. Вся логика
«кнопок» — в core.py. httpx сам подхватывает HTTPS_PROXY из окружения
(на сервере бот ходит к Todoist через Xray-мост), поэтому прокси тут не настраиваем.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

API_BASE = "https://api.todoist.com/api/v1"


class TodoistError(RuntimeError):
    """Ошибка обращения к Todoist (не 2xx) с понятным текстом."""


class TodoistClient:
    """Обёртка над httpx.AsyncClient. Использовать как async context manager."""

    def __init__(self, token: str | None = None, timeout: float = 30.0) -> None:
        token = token or os.environ.get("TODOIST_API_TOKEN", "")
        if not token:
            raise TodoistError("Нет токена Todoist: задайте переменную TODOIST_API_TOKEN.")
        self._http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            trust_env=True,  # подхватить HTTPS_PROXY на сервере
        )

    async def __aenter__(self) -> "TodoistClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as err:
            body = err.response.text[:300]
            raise TodoistError(f"Todoist {err.response.status_code} на {method} {path}: {body}") from err
        except httpx.HTTPError as err:
            raise TodoistError(f"Сеть недоступна на {method} {path}: {err}") from err
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # Короткие удобные обёртки
    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self.request("POST", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", path, **kwargs)

    async def get_paginated(self, path: str, params: dict[str, Any] | None = None,
                            key: str = "results", cap: int = 300) -> list[dict[str, Any]]:
        """Собрать все страницы (Todoist отдаёт `results` + `next_cursor`)."""
        params = dict(params or {})
        items: list[dict[str, Any]] = []
        while True:
            data = await self.get(path, params=params)
            if isinstance(data, list):
                return data
            items.extend(data.get(key, []))
            cursor = data.get("next_cursor")
            if not cursor or len(items) >= cap:
                return items[:cap]
            params["cursor"] = cursor
