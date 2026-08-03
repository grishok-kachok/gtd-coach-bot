"""Расшифровка голосовых. Голос — основной вход этого бота.

Два сервиса подряд, а не один. Оба крутят один и тот же Whisper от OpenAI,
разница — кто его запускает и почём:
    Groq    https://api.groq.com/openai/v1/audio/transcriptions   whisper-large-v3-turbo
    OpenAI  https://api.openai.com/v1/audio/transcriptions        whisper-1
У Groq щедрый бесплатный тир, поэтому обычная расшифровка владельцу не стоит
ничего (сверх тира — $0.04 за час аудио против $0.36 у OpenAI: своё железо LPU,
а не другая нейросеть). Поэтому Groq основной, а OpenAI — запасной аэродром:
включается на первой же осечке основного. API совместимые, prompt поддерживают
оба (проверено 2026-07-27).

⚠️ Оба блокируют РФ и работают только через HTTPS_PROXY (Xray-мост).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

import httpx

from .retry import retry_network

log = logging.getLogger(__name__)

DEFAULT_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_STT_MODEL = "whisper-large-v3-turbo"
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_STT_MODEL = "whisper-1"

# Сколько раз мучаем каждый сервис, прежде чем идти дальше. Основному дана одна
# попытка: бесплатный тир Groq капризничает (владелец ловил осечки и на маке в
# MacWhisper), и уговаривать его повторами дольше, чем сразу спросить OpenAI.
# Все остальные попытки — запасному, он и доводит дело до конца.
PRIMARY_ATTEMPTS = 1
FALLBACK_ATTEMPTS = 9

# Подсказка распознавателю: слова, которые он регулярно калечит.
#
# Общая часть — здесь, личная (имена, города, названия проектов) — в мозге.
# Иначе список неизбежно зарастает личными именами одного человека и становится
# вредным для всех остальных: чужое имя в подсказке заставляет распознаватель
# слышать его там, где его не было.
БАЗОВЫЙ_СЛОВАРЬ = (
    "Todoist, Клод, Claude, дедлайн, задача, проект, созвон, эфир"
)
ОБЩЕЕ = "Разговор о делах и планах. Возможные слова: "

# Whisper слушает подсказку не целиком: на неё у него ~224 токена, остальное
# отрезается — и отрезается молча. Настоящего счётчика токенов в боте нет,
# заводить ради одного списка слов целую зависимость незачем, поэтому считаем
# символами и с запасом в свою сторону: на кириллице токен редко короче
# полутора символов, значит 224 токена — это никак не меньше 336 символов.
# Ошибка такого счёта безопасна: подсказка выйдет короче разрешённого, а не
# длиннее, и обрежем её мы сами и по своему порядку, а не сервис по своему.
ПОТОЛОК_СИМВОЛОВ = 336


def собрать_словарь(глоссарий: list[str] | None = None) -> list[str]:
    """Два источника слов в один список, без дублей.

    Источников ровно два, и это решение, а не недоделка. Общие слова живут
    в коде: они одинаковы у всех, и человеку в них лазить незачем. Свои слова
    живут **в одном** месте — в глоссарии памяти, куда их пишет сам коуч по
    фразе в телеграме. Третьего списка нет намеренно: он и был бедой, из-за
    которой слово, описанное в памяти, до распознавателя не доезжало.

    Порядок — он же старшинство при обрезке: сперва общие, потом свои.
    Дубли сверяются без учёта регистра, пишется первое написание.
    """
    итог: list[str] = []
    видели: set[str] = set()
    for источник in (БАЗОВЫЙ_СЛОВАРЬ.split(","), глоссарий or []):
        for слово in источник:
            чистое = str(слово).strip()
            if not чистое or чистое.casefold() in видели:
                continue
            видели.add(чистое.casefold())
            итог.append(чистое)
    return итог


def подсказка(глоссарий: list[str] | None = None) -> str:
    """Что подсказать распознавателю: общие слова плюс свои из глоссария."""
    слова = собрать_словарь(глоссарий)
    while len(слова) > 1 and len(ОБЩЕЕ) + len(", ".join(слова)) + 1 > ПОТОЛОК_СИМВОЛОВ:
        слова.pop()
    return ОБЩЕЕ + ", ".join(слова) + "."


HINT = подсказка()


class Service:
    """Один сервис распознавания: куда стучаться, каким ключом, какой моделью."""

    def __init__(self, name: str, api_key: str, url: str, model: str) -> None:
        self.name = name
        self.api_key = api_key
        self.url = url
        self.model = model


class VoiceRecognizer:
    def __init__(
        self,
        primary: Service,
        fallback: Service | None = None,
        proxy: str | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.proxy = proxy

    async def transcribe(
        self,
        audio: bytes,
        filename: str = "voice.ogg",
        on_retry: Callable[[int, BaseException], Awaitable[None]] | None = None,
        on_switch: Callable[[str, str], Awaitable[None]] | None = None,
        глоссарий: list[str] | None = None,
    ) -> str:
        """Расшифровать голосовое: сначала основным сервисом, потом запасным.

        on_retry  — зовём перед каждой повторной попыткой.
        on_switch — зовём при переходе на запасной сервис (кто отвалился, к кому идём).
        глоссарий — свои слова человека из памяти.
        """
        # ⚠️ Groq определяет формат ПО РАСШИРЕНИЮ ИМЕНИ, а не по содержимому, и `.oga`
        # в его списке нет: [flac mp3 mp4 mpeg mpga m4a ogg opus wav webm]. Telegram
        # отдаёт голосовые именно как `.oga` — OpenAI это принимал, Groq отвечает
        # 400 «file must be one of the following types». Файл при этом валидный:
        # тот же байт-в-байт под именем `.ogg` распознаётся идеально (проверено 2026-07-27).
        # Поэтому нормализуем расширение перед отправкой.
        if filename.lower().endswith(".oga"):
            filename = filename[:-4] + ".ogg"

        plan = [(self.primary, PRIMARY_ATTEMPTS)]
        if self.fallback is not None:
            plan.append((self.fallback, FALLBACK_ATTEMPTS))
        else:
            # Запасного нет — значит все попытки достаются единственному сервису.
            plan = [(self.primary, PRIMARY_ATTEMPTS + FALLBACK_ATTEMPTS)]

        for index, (service, attempts) in enumerate(plan):
            last = index == len(plan) - 1
            try:
                return await retry_network(
                    lambda service=service: self._ask_service(
                        service, audio, filename, подсказка(глоссарий)),
                    what=f"расшифровка через {service.name}",
                    attempts=attempts,
                    on_retry=on_retry,
                )
            except Exception as error:
                if last:
                    raise
                # Причина неважна: основной сервис не справился — пробуем запасного.
                # Он другой конторы, другим маршрутом и терпимее к формату файла.
                nxt = plan[index + 1][0]
                log.warning(
                    "%s не справился (%s: %s) — переключаюсь на %s",
                    service.name, type(error).__name__, error, nxt.name,
                )
                if on_switch is not None:
                    try:
                        await on_switch(service.name, nxt.name)
                    except Exception:  # уведомление не должно ронять расшифровку
                        log.exception("не удалось сообщить о переключении сервиса")
        raise AssertionError("недостижимо")  # цикл выходит только через return или raise

    async def _ask_service(self, service: Service, audio: bytes, filename: str,
                           hint: str = HINT) -> str:
        """Одна попытка: отправить аудио сервису распознавания и забрать текст."""
        async with httpx.AsyncClient(timeout=120, proxy=self.proxy) as http:
            response = await http.post(
                service.url,
                headers={"Authorization": f"Bearer {service.api_key}"},
                files={"file": (filename, audio, "audio/ogg")},
                data={"model": service.model, "language": "ru", "prompt": hint},
            )
            response.raise_for_status()
            return (response.json().get("text") or "").strip()
