"""Повтор — только того, что чинится само.

Отдельный тест, потому что связка неочевидна: клиент Todoist заворачивает
httpx-ошибку в свой тип, и «повторяем всё сетевое» на него не срабатывало.
Из-за этого 01.08.2026 обрыв соединения посреди работы канала комментариев
выглядел как «ничего не изменилось».
"""

import httpx
import pytest
from telegram.error import BadRequest

from src.retry import is_retryable
from todoist_mcp.client import TodoistError


def test_сетевую_ошибку_todoist_повторяем():
    assert is_retryable(TodoistError("Сеть недоступна", retryable=True)) is True


def test_отказ_по_существу_не_повторяем():
    assert is_retryable(TodoistError("Todoist 400: поле кривое")) is False


def test_прежние_правила_целы():
    assert is_retryable(httpx.ConnectTimeout("долго")) is True
    assert is_retryable(BadRequest("битый file_id")) is False
