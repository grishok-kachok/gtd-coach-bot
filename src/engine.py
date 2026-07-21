"""Движок коуча: Claude Agent SDK поверх памяти и инструментов.

Слой ничего не знает про Telegram — на вход текст, на выход текст. Когда
появится агент-шлюз (см. .bpd этапа 04), сюда придут другие потребители.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from .sessions import SessionStorage
from .todoist import build_todoist_server

log = logging.getLogger(__name__)

# Инструменты памяти + git (Bash ограничен списком разрешённых команд в settings)
MEMORY_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"]


class CoachEngine:
    """Один непрерывный разговор с коучем."""

    def __init__(
        self,
        brain_dir: Path,
        session_storage: SessionStorage,
        system_prompt: str,
        todoist_token: str,
        model: str,
        effort: str = "medium",
    ) -> None:
        self.brain_dir = brain_dir
        self.sessions = session_storage
        self.system_prompt = system_prompt
        self.model = model
        self.effort = effort
        self.todoist_server = build_todoist_server(todoist_token)

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            cwd=str(self.brain_dir),
            model=self.model,
            effort=self.effort,
            resume=resume,
            permission_mode="bypassPermissions",
            mcp_servers={"todoist": self.todoist_server},
            allowed_tools=MEMORY_TOOLS + ["mcp__todoist"],
            setting_sources=["project"],
        )

    async def ask(self, text: str, memory: str = "") -> str:
        """Задать вопрос, продолжая прошлый разговор.

        Разговор начинается заново каждую ночь, поэтому в первый запрос новой
        сессии подкладываем выжимки прошлых дней — иначе коуч проснётся с
        чистой головой и заставит Василия пересказывать вчерашнее.
        """
        resume = self.sessions.load()
        prompt = text if resume else self._with_memory(text, memory)
        try:
            return await self._run(prompt, resume)
        except Exception:
            if resume is None:
                raise
            # Сессия могла протухнуть (например, память пересоздана) — начинаем свежую,
            # чтобы бот не онемел из-за одной битой ссылки.
            log.warning("не удалось продолжить сессию %s, начинаю новую", resume, exc_info=True)
            self.sessions.clear()
            return await self._run(self._with_memory(text, memory), None)

    @staticmethod
    def _with_memory(text: str, memory: str) -> str:
        if not memory.strip():
            return text
        return (
            "<память_прошлых_дней>\n"
            "Это твои же выжимки прошлых разговоров с Василием — самые свежие внизу.\n"
            "Опирайся на них молча: не пересказывай их и не ссылайся на них вслух.\n\n"
            f"{memory}\n"
            "</память_прошлых_дней>\n\n"
            f"{text}"
        )

    async def _run(self, text: str, resume: str | None) -> str:
        parts: list[str] = []
        async with ClaudeSDKClient(options=self._options(resume)) as client:
            await client.query(text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    if message.session_id:
                        self.sessions.save(message.session_id)
                    if message.is_error:
                        log.error("движок вернул ошибку: %s", message.result)

        answer = "\n".join(p.strip() for p in parts if p.strip())
        return answer or "…(коуч промолчал — похоже, что-то пошло не так)"
