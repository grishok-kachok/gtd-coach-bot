"""Ночная причёска истории мозга: день сворачивается в один осмысленный коммит.

Сторож на сервере коммитит быстро и тупо — «досохранение».
Иначе нельзя: он срабатывает на каждое сохранение файла и не может звать модель.
Зато ночью модель уже работает над выжимкой дня — она же и подписывает историю
по-человечески, глядя на реальный diff.

Осторожность здесь важнее красоты: история переписывается, а это единственная
копия памяти Василия. Поэтому — только когда всё синхронизировано, только
--force-with-lease, и содержимое файлов при этом не меняется ни на байт.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

log = logging.getLogger(__name__)

# Коммиты, которые не жалко склеить: машинные подписи без смысла.
AUTO_PREFIXES = ("Автосохранение мозга", "Коуч (телеграм):", "Коуч (сервер):")

PROMPT = """Ты подписываешь один коммит, в который сворачивается день работы с памятью коуча.

Вот что менялось за день (git diff --stat):

{stat}

Вот сами изменения (обрезаны, если длинные):

{diff}

Прежние подписи коммитов за день (машинные, их и заменяем):

{subjects}

Напиши сообщение коммита по-русски:
- Первая строка — до 70 символов, суть дня по существу. Без слова «коммит», без даты в начале.
- Пустая строка.
- Затем 2-5 строк: что именно изменилось в памяти и почему это важно. Конкретно — имена, решения, сроки.
- Не выдумывай того, чего нет в diff.
- Не пиши markdown-заголовки и списки со звёздочками. Обычный текст, строки с дефисом допустимы.

Верни только текст сообщения, без обрамления.
"""


class HistoryTidier:
    def __init__(self, repo_dir: Path, model: str = "claude-fable-5") -> None:
        self.repo_dir = repo_dir
        self.model = model

    async def _git(self, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(self.repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await process.communicate()
        return process.returncode or 0, out.decode("utf-8", "replace").strip()

    async def _safe_to_rewrite(self) -> bool:
        """История переписывается только когда все три копии сходятся."""
        code, status = await self._git("status", "--porcelain")
        if code or status:
            log.info("причёска отменена: есть несохранённые правки")
            return False

        await self._git("fetch", "-q")
        _, local = await self._git("rev-parse", "HEAD")
        _, remote = await self._git("rev-parse", "@{u}")
        if local != remote:
            log.info("причёска отменена: локальная и удалённая история разошлись")
            return False
        return True

    async def _day_commits(self, day: date) -> tuple[str | None, list[str]]:
        """Коммиты за сутки и коммит-основание под ними."""
        since = f"{day.isoformat()} 00:00"
        until = f"{day.isoformat()} 23:59:59"
        code, out = await self._git(
            "log", f"--since={since}", f"--until={until}", "--format=%H%x1f%s", "--reverse"
        )
        if code or not out:
            return None, []

        rows = [line.split("\x1f", 1) for line in out.splitlines() if "\x1f" in line]
        if len(rows) < 2:
            return None, []  # один коммит за день сворачивать не во что

        first_hash = rows[0][0]
        subjects = [subject for _, subject in rows]

        # Дно, на которое ляжет свёрнутый день
        code, base = await self._git("rev-parse", f"{first_hash}^")
        if code:
            return None, []  # первый коммит репозитория — не трогаем
        return base, subjects

    async def _compose_message(self, base: str, subjects: list[str]) -> str | None:
        _, stat = await self._git("diff", "--stat", base, "HEAD")
        _, diff = await self._git("diff", base, "HEAD")
        if len(diff) > 24000:
            diff = diff[:24000] + "\n… (обрезано)"

        prompt = PROMPT.format(
            stat=stat or "(нет изменений)",
            diff=diff or "(пусто)",
            subjects="\n".join(f"- {s}" for s in subjects),
        )
        options = ClaudeAgentOptions(
            model=self.model,
            effort="medium",
            tools=[],
            system_prompt="Ты аккуратно подписываешь историю изменений. Пишешь по-русски, по делу, без воды.",
        )
        parts: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage) and message.is_error:
                log.error("подпись коммита не удалась: %s", message.result)
                return None
        return "\n".join(p.strip() for p in parts if p.strip()).strip() or None

    async def tidy(self, day: date) -> str | None:
        """Свернуть день в один коммит. Возвращает первую строку подписи."""
        if not await self._safe_to_rewrite():
            return None

        base, subjects = await self._day_commits(day)
        if not base:
            return None

        # Осмысленные подписи (мои и человеческие) не теряем — переносим в тело.
        kept = [s for s in subjects if not s.startswith(AUTO_PREFIXES)]

        message = await self._compose_message(base, subjects)
        if not message:
            return None

        body = f"{message}\n\nСвёрнуто коммитов: {len(subjects)} (за {day.isoformat()})"
        if kept:
            body += "\nСреди них:\n" + "\n".join(f"- {s}" for s in kept)

        _, before_tree = await self._git("rev-parse", "HEAD^{tree}")

        code, out = await self._git("reset", "--soft", base)
        if code:
            log.error("reset не прошёл: %s", out)
            return None

        code, out = await self._git("commit", "-q", "-m", body)
        if code:
            log.error("свёрнутый коммит не создался: %s", out)
            await self._git("reset", "--hard", "ORIG_HEAD")
            return None

        # Содержимое обязано остаться прежним — иначе откатываемся, не раздумывая.
        _, after_tree = await self._git("rev-parse", "HEAD^{tree}")
        if before_tree != after_tree:
            log.error("свёртка изменила содержимое — откат")
            await self._git("reset", "--hard", "ORIG_HEAD")
            return None

        code, out = await self._git("push", "--force-with-lease", "-q")
        if code:
            log.warning("force-push не прошёл, откатываюсь: %s", out)
            await self._git("reset", "--hard", "ORIG_HEAD")
            return None

        headline = body.splitlines()[0]
        log.info("история за %s свёрнута (%d → 1): %s", day, len(subjects), headline)
        return headline
