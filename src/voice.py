"""Расшифровка голосовых. Голос — основной вход этого бота."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"

# Имена и слова, которые Whisper регулярно калечит в речи Василия.
HINT = (
    "Разговор о делах и планах. Возможные слова: Todoist, Клод, Claude, "
    "АкадемИИ, Бали, Таня, Татьяна, поток, эфир, домашки, рилс, карусель, "
    "Инстаграм, ИП, эквайринг, дедлайн."
)


class VoiceRecognizer:
    def __init__(self, api_key: str, proxy: str | None = None, model: str = "whisper-1") -> None:
        self.api_key = api_key
        self.proxy = proxy
        self.model = model

    async def transcribe(self, audio: bytes, filename: str = "voice.oga") -> str:
        async with httpx.AsyncClient(timeout=120, proxy=self.proxy) as http:
            response = await http.post(
                WHISPER_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (filename, audio, "audio/ogg")},
                data={"model": self.model, "language": "ru", "prompt": HINT},
            )
            response.raise_for_status()
            return (response.json().get("text") or "").strip()
