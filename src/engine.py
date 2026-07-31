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
    TaskNotificationMessage,
    TextBlock,
)

from .gcal import build_calendar_server
from .sessions import SessionStorage
from .todoist import build_todoist_server
from .dashboard_tool import build_dashboard_server
from .wishes import build_wishes_server

log = logging.getLogger(__name__)

# Инструменты памяти + git (Bash ограничен списком разрешённых команд в settings)
MEMORY_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"]

# Помощников коуч нанимать не может — и это не придирка, а свойство архитектуры.
# Движок здесь живёт ровно один ответ: `async with ClaudeSDKClient` закрывается,
# и всё, что он породил, умирает вместе с ним. Фоновый агент доработать не успевает
# НИКОГДА, а его осиротевшая запись потом присылает уведомление на каждом
# возобновлении сессии — и это уведомление съедает ход целиком (30.07.2026).
# Снимать запрет можно только вместе с переходом на долгоживущий клиент.
FORBIDDEN_TOOLS = ["Task"]

# Роль коуча — плагин gtd-coach, смонтированный только на чтение (том /plugin).
# Маркетплейс здесь не годится: движок в контейнере принимает лишь локальный путь
# (SdkPluginConfig = {"type": "local", "path": str}, проверено на SDK 0.2.124).
# Отсюда же конституция: PROMPT_FILE указывает внутрь этой папки.
PLUGINS = [{"type": "local", "path": "/plugin"}]


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
        calendar: dict | None = None,
        extra_dirs: list[Path] | None = None,
        dashboard: dict | None = None,
    ) -> None:
        self.brain_dir = brain_dir
        self.sessions = session_storage
        self.system_prompt = system_prompt
        self.model = model
        self.effort = effort
        # Папки за пределами мозга, куда движку тоже нужен доступ. Сейчас это
        # присланные картинки: в мозге им не место (он репозиторий), а Read по
        # умолчанию видит только cwd — без этого списка он до них не дотянется.
        self.extra_dirs = [str(path) for path in (extra_dirs or [])]
        self.todoist_server = build_todoist_server(todoist_token)
        # Заявки: «хочу, чтобы ты умел X» — коуч записывает, а не делает.
        self.wishes_server = build_wishes_server(brain_dir, todoist_token)
        # Дашборд — файл в телеграм. Отправка обязана случаться, значит инструмент.
        self.dashboard_server = build_dashboard_server(**dashboard) if dashboard else None
        # Календарь подключаем только когда заданы креды — без них бот работает как прежде.
        self.calendar_server = build_calendar_server(**calendar) if calendar else None

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        mcp_servers = {"todoist": self.todoist_server, "wishes": self.wishes_server}
        allowed = MEMORY_TOOLS + ["mcp__todoist", "mcp__wishes"]
        if self.calendar_server is not None:
            mcp_servers["calendar"] = self.calendar_server
            allowed = allowed + ["mcp__calendar"]
        if self.dashboard_server is not None:
            mcp_servers["dashboard"] = self.dashboard_server
            allowed = allowed + ["mcp__dashboard"]
        return ClaudeAgentOptions(
            system_prompt=self.system_prompt,
            cwd=str(self.brain_dir),
            model=self.model,
            effort=self.effort,
            resume=resume,
            permission_mode="bypassPermissions",
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            add_dirs=self.extra_dirs,
            disallowed_tools=FORBIDDEN_TOOLS,
            setting_sources=["project"],
            plugins=PLUGINS,
        )

    async def ask(self, text: str, memory: str = "", agenda: str = "") -> str:
        """Задать вопрос, продолжая прошлый разговор.

        Разговор начинается заново каждую ночь, поэтому в первый запрос новой
        сессии подкладываем выжимки прошлых дней — иначе коуч проснётся с
        чистой головой и заставит Василия пересказывать вчерашнее.

        Сводка дел подкладывается к КАЖДОЙ реплике, а не только к первой: дела
        меняются в течение дня, в том числе руками самого коуча. Это пара сотен
        токенов — цена того, чтобы он никогда не рассуждал о вчерашней картине.
        """
        resume = self.sessions.load()
        prompt = self._wrap(text, memory if not resume else "", agenda)
        try:
            return await self._run(prompt, resume)
        except Exception:
            if resume is None:
                raise
            # Сессия могла протухнуть (например, память пересоздана) — начинаем свежую,
            # чтобы бот не онемел из-за одной битой ссылки.
            log.warning("не удалось продолжить сессию %s, начинаю новую", resume, exc_info=True)
            self.sessions.clear()
            return await self._run(self._wrap(text, memory, agenda), None)

    @staticmethod
    def _wrap(text: str, memory: str, agenda: str) -> str:
        blocks = []
        if memory.strip():
            blocks.append(
                "<память_прошлых_дней>\n"
                "Это твои же выжимки прошлых разговоров с Василием — самые свежие внизу.\n"
                "Опирайся на них молча: не пересказывай их и не ссылайся на них вслух.\n\n"
                f"{memory}\n"
                "</память_прошлых_дней>"
            )
        if agenda.strip():
            blocks.append(
                "<дела_сейчас>\n"
                "Свежий агрегат из Todoist, собран кодом только что. Todoist — дом дел,\n"
                "это его отражение: правится источник, а не отражение.\n\n"
                f"{agenda}\n"
                "</дела_сейчас>"
            )
        blocks.append(text)
        return "\n\n".join(blocks)

    async def _run(self, text: str, resume: str | None, *, second_try: bool = False) -> str:
        parts: list[str] = []
        heard_model = False
        notified = False
        subtype = None
        async with ClaudeSDKClient(options=self._options(resume)) as client:
            await client.query(text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    heard_model = True
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(message, TaskNotificationMessage):
                    # Движок докладывает о судьбе фонового агента. Нам он не нужен,
                    # но факт важен: такой доклад закрывает ход, не дав модели слова.
                    notified = True
                    log.warning("движок прислал уведомление о фоновой задаче: %r", message)
                elif isinstance(message, ResultMessage):
                    if message.session_id:
                        self.sessions.save(message.session_id)
                    subtype = message.subtype
                    if message.is_error:
                        log.error("движок вернул ошибку: %s", message.result)

        answer = "\n".join(p.strip() for p in parts if p.strip())
        if answer:
            return answer

        # Пустой ход. Раньше здесь молча подставлялась заглушка, и в docker logs не
        # оставалось ни следа: 30.07.2026 бот дважды ответил «коуч промолчал», а
        # найти причину удалось только по транскриптам сессии. Теперь — громко.
        log.error(
            "движок вернул пустой ответ: модель %s, уведомление о фоновой задаче %s, "
            "subtype=%s, повторная попытка=%s",
            "говорила" if heard_model else "молчала",
            "было" if notified else "не приходило",
            subtype,
            second_try,
        )
        if not heard_model and not second_try:
            # Модель не сказала ни слова — значит ход съело что-то служебное, а не она
            # сама. Повторяем ровно один раз и по свежей закладке: уведомление уже
            # доставлено и второй раз не придёт. Если модель говорила, но текста не
            # дала, — не повторяем, иначе рискуем сделать её работу дважды.
            log.warning("повторяю запрос один раз — ход съело служебное сообщение")
            return await self._run(text, self.sessions.load() or resume, second_try=True)
        return "…(коуч промолчал — похоже, что-то пошло не так)"
