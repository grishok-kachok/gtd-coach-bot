"""Повтор сетевых операций.

Голос — основной вход этого бота, а весь путь голосового идёт через Xray-мост:
скачивание файла из Telegram и расшифровка в Groq. Мост иногда моргает — один
таймаут не должен означать «сообщение потеряно». Поэтому операцию повторяем.

Повторяем ТОЛЬКО то, что может починиться само: таймауты, обрывы связи, отказ
прокси, 429 и 5xx. Отказ по существу (битый файл, неверный ключ, слишком
большой файл) повторять бессмысленно — такая ошибка проходит наружу сразу.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

import httpx
from telegram.error import BadRequest, Forbidden, InvalidToken
from telegram.error import NetworkError as TelegramNetworkError
from telegram.error import RetryAfter as TelegramRetryAfter

log = logging.getLogger(__name__)

T = TypeVar("T")

ATTEMPTS = 10  # столько раз пробуем, прежде чем показать ошибку владельцу
BASE_DELAY = 1.0  # первая пауза, дальше удвоение
MAX_DELAY = 15.0  # потолок паузы: 1, 2, 4, 8, 15, 15, ... — всего около минуты ожиданий

# ⚠️ В python-telegram-bot BadRequest и Forbidden — НАСЛЕДНИКИ NetworkError
# (проверено на 22.8), поэтому «ретраим всё сетевое» ловило бы и битый file_id.
# Эти три исключаем явно, до проверки на NetworkError.
HOPELESS = (BadRequest, Forbidden, InvalidToken)


class GaveUp(RuntimeError):
    """Все попытки израсходованы. Несёт в себе исходную причину — её и показываем."""

    def __init__(self, what: str, attempts: int, cause: BaseException) -> None:
        self.what = what
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"{what}: не вышло за {attempts} попыт(ок), последняя ошибка — "
            f"{type(cause).__name__}: {cause}"
        )


def is_retryable(error: BaseException) -> bool:
    """Стоит ли повторять — то есть «связь моргнула», а не «запрос неправильный»."""
    if isinstance(error, HOPELESS):
        return False
    if isinstance(error, TelegramRetryAfter):  # Telegram сам просит подождать
        return True
    if isinstance(error, TelegramNetworkError):  # таймаут/обрыв по пути к Telegram
        return True
    if isinstance(error, httpx.HTTPStatusError):  # ответ сервиса расшифровки
        code = error.response.status_code
        return code == 429 or code >= 500
    # Таймауты, обрывы, отказ прокси, поломка протокола — всё это TransportError.
    return isinstance(error, httpx.TransportError)


def _pause(attempt: int, error: BaseException) -> float:
    """Сколько ждать перед следующей попыткой."""
    if isinstance(error, TelegramRetryAfter):
        return float(error.retry_after) + 1.0  # Telegram назвал срок — уважаем его
    delay = min(BASE_DELAY * 2 ** (attempt - 1), MAX_DELAY)
    return delay + random.uniform(0, 0.5)  # разброс, чтобы попытки не били в такт


async def retry_network(
    action: Callable[[], Awaitable[T]],
    *,
    what: str,
    attempts: int = ATTEMPTS,
    on_retry: Callable[[int, BaseException], Awaitable[None]] | None = None,
) -> T:
    """Выполнить `action`, повторяя при сетевых сбоях.

    action   — функция без аргументов, возвращающая корутину (каждая попытка
               вызывает её заново: новое соединение вместо протухшего).
    what     — что делаем, человеческими словами; идёт в лог и в текст ошибки.
    on_retry — необязательный крючок: зовём перед паузой (например, чтобы
               предупредить владельца, что связь шалит и мы ещё пробуем).
    """
    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except Exception as error:
            if not is_retryable(error):
                raise
            if attempt == attempts:
                raise GaveUp(what, attempts, error) from error
            delay = _pause(attempt, error)
            log.warning(
                "%s: попытка %d/%d сорвалась (%s: %s), повтор через %.1f с",
                what, attempt, attempts, type(error).__name__, error, delay,
            )
            if on_retry is not None:
                try:
                    await on_retry(attempt, error)
                except Exception:  # крючок не должен ронять саму операцию
                    log.exception("крючок on_retry сорвался")
            await asyncio.sleep(delay)
    raise AssertionError("недостижимо")  # цикл всегда выходит через return или raise
