"""Синхронизация мозга: та же папка памяти, что у коуча в Claude Code.

Перед разговором подтягиваем свежее, после — отдаём написанное. Мозг один,
каналов два, поэтому расходиться им нельзя.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Brain:
    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    async def _git(self, *args: str, check: bool = False) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = output.decode("utf-8", "replace").strip()
        code = process.returncode or 0
        if code and check:
            raise RuntimeError(f"git {' '.join(args)}: {text}")
        return code, text

    async def pull(self) -> None:
        """Подтянуть чужие правки. Свои локальные при конфликте не теряем."""
        code, text = await self._git("pull", "--rebase", "--autostash")
        if code:
            log.warning("git pull не прошёл: %s", text)
            await self._git("rebase", "--abort")

    async def push(self, reason: str) -> bool:
        """Закоммитить и отправить, если что-то поменялось."""
        _, status = await self._git("status", "--porcelain")
        if not status:
            return False
        await self._git("add", "-A")
        await self._git("commit", "-m", f"Коуч (телеграм): {reason}")
        code, text = await self._git("push")
        if code:
            log.warning("git push не прошёл: %s", text)
            return False
        log.info("мозг обновлён: %s", reason)
        return True
