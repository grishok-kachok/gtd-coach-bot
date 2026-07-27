"""Расшифровка голосовых. Голос — основной вход этого бота."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

# Адрес сервиса распознавания. Оба варианта — один и тот же Whisper от OpenAI,
# отличается лишь тот, кто его запускает:
#   OpenAI  https://api.openai.com/v1/audio/transcriptions      модель whisper-1
#   Groq    https://api.groq.com/openai/v1/audio/transcriptions  модель whisper-large-v3-turbo
# Groq примерно в 9 раз дешевле ($0.04 против $0.36 за час аудио) — своё железо (LPU),
# а не другая нейросеть. API OpenAI-совместимый, prompt поддерживается (проверено 2026-07-27).
# ⚠️ Groq блокирует РФ: напрямую отдаёт 403, работает только через HTTPS_PROXY.
DEFAULT_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_STT_MODEL = "whisper-large-v3-turbo"
OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"

# Имена и слова, которые Whisper регулярно калечит в речи Василия.
HINT = (
    "Разговор о делах и планах. Возможные слова: Todoist, Клод, Claude, "
    "АкадемИИ, Бали, Таня, Татьяна, поток, эфир, домашки, рилс, карусель, "
    "Инстаграм, ИП, эквайринг, дедлайн."
)


class VoiceRecognizer:
    def __init__(
        self,
        api_key: str,
        proxy: str | None = None,
        model: str = DEFAULT_STT_MODEL,
        url: str = DEFAULT_STT_URL,
    ) -> None:
        self.api_key = api_key
        self.proxy = proxy
        self.model = model
        self.url = url

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        # ⚠️ Groq определяет формат ПО РАСШИРЕНИЮ ИМЕНИ, а не по содержимому, и `.oga`
        # в его списке нет: [flac mp3 mp4 mpeg mpga m4a ogg opus wav webm]. Telegram
        # отдаёт голосовые именно как `.oga` — OpenAI это принимал, Groq отвечает
        # 400 «file must be one of the following types». Файл при этом валидный:
        # тот же байт-в-байт под именем `.ogg` распознаётся идеально (проверено 2026-07-27).
        # Поэтому нормализуем расширение перед отправкой.
        if filename.lower().endswith(".oga"):
            filename = filename[:-4] + ".ogg"

        async with httpx.AsyncClient(timeout=120, proxy=self.proxy) as http:
            response = await http.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (filename, audio, "audio/ogg")},
                data={"model": self.model, "language": "ru", "prompt": HINT},
            )
            response.raise_for_status()
            return (response.json().get("text") or "").strip()
