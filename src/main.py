"""Телеграм-канал коуча: приём голоса и текста, два пинга в день.

Здесь только доставка. Всё, что думает, живёт в engine.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .archive import Archive
from .brain import Brain
from .digest import Digester
from .engine import CoachEngine
from .sessions import SessionStorage
from .voice import VoiceRecognizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("coach")

TELEGRAM_LIMIT = 4000
MOSCOW = ZoneInfo("Europe/Moscow")

MORNING_PROMPT = (
    "Наступило утро. Поздоровайся коротко, посмотри задачи на сегодня и ближайшие "
    "дни в Todoist и спроси Василия, что сегодня главное. Не вываливай весь список — "
    "назови то, что действительно требует внимания сегодня, с опорой на его цели "
    "из памяти. Максимум несколько строк."
)

EVENING_PROMPT = (
    "Наступил вечер. Спроси Василия, что он сегодня сделал и что перенести. "
    "Загляни в задачи, у которых сегодняшний срок, — по ним и спрашивай. "
    "Коротко, по-человечески, без отчётного тона."
)


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default or "")
    if required and not value:
        raise SystemExit(f"Не задана переменная окружения {name}")
    return value


class CoachBot:
    def __init__(self) -> None:
        self.owner_id = int(env("OWNER_TELEGRAM_ID", required=True))
        self.brain_dir = Path(env("BRAIN_DIR", "/brain"))
        state_dir = Path(env("STATE_DIR", "/state"))

        self.brain = Brain(self.brain_dir)
        self.engine = CoachEngine(
            brain_dir=self.brain_dir,
            session_storage=SessionStorage(state_dir / "session_id"),
            system_prompt=self._system_prompt(),
            todoist_token=env("TODOIST_API_TOKEN", required=True),
            model=env("COACH_MODEL", "claude-fable-5"),
            effort=env("COACH_EFFORT", "medium"),
        )
        self.voice = VoiceRecognizer(
            api_key=env("OPENAI_API_KEY", required=True),
            proxy=env("HTTPS_PROXY") or None,
        )
        self.archive = Archive(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        self.memory_days = int(env("MEMORY_DAYS", "30"))
        self.digester = Digester(self.brain_dir, self.archive, model=env("DIGEST_MODEL", "claude-fable-5"))
        # Один разговор — значит одна очередь. Иначе две сессии подерутся за resume.
        self.lock = asyncio.Lock()

    def _system_prompt(self) -> str:
        path = Path(env("PROMPT_FILE", "/app/prompts/coach.md"))
        return path.read_text(encoding="utf-8")

    # --- вспомогательное ---

    def _mine(self, update: Update) -> bool:
        user = update.effective_user
        if user and user.id == self.owner_id:
            return True
        log.warning("чужой стучится: id=%s", user.id if user else "?")
        return False

    async def _think_and_reply(
        self, text: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, channel: str = "text"
    ) -> None:
        async with self.lock:
            await self.archive.add_message("vasiliy", channel, text, self.engine.sessions.load())
            typing = asyncio.create_task(self._keep_typing(chat_id, context))
            try:
                await self.brain.pull()
                # Выжимки прошлых дней движок подставит сам, если разговор начинается заново.
                memory = await asyncio.to_thread(self.archive.recent_digests, self.memory_days)
                answer = await self.engine.ask(text, memory)
                await self.brain.push(text[:60].replace("\n", " "))
            except Exception as error:  # доставляем боль владельцу, а не в лог-файл
                log.exception("сорвалось на ответе")
                answer = f"Сломался: {type(error).__name__}: {error}"
            finally:
                typing.cancel()
            await self.archive.add_message("coach", channel, answer, self.engine.sessions.load())

        for chunk in self._split(answer):
            await context.bot.send_message(chat_id=chat_id, text=chunk)

    async def _keep_typing(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            while True:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _split(text: str) -> list[str]:
        chunks, current = [], ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > TELEGRAM_LIMIT:
                chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)
        return chunks or ["…"]

    # --- обработчики ---

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        await self._think_and_reply(update.message.text, update.effective_chat.id, context)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        chat_id = update.effective_chat.id
        message = update.message
        source = message.voice or message.audio or message.video_note
        try:
            telegram_file = await context.bot.get_file(source.file_id)
            audio = bytes(await telegram_file.download_as_bytearray())
            text = await self.voice.transcribe(audio)
        except Exception as error:
            log.exception("расшифровка не удалась")
            await context.bot.send_message(chat_id=chat_id, text=f"Не разобрал голос: {error}")
            return

        if not text:
            await context.bot.send_message(chat_id=chat_id, text="Тишина — ничего не разобрал.")
            return

        await context.bot.send_message(chat_id=chat_id, text=f"🎙 {text}")
        await self._think_and_reply(text, chat_id, context, channel="voice")

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        await update.message.reply_text(
            "Я на связи. Наговаривай или пиши — разберём дела.\n"
            "Пингую утром в 10:00 и вечером в 20:00.\n"
            "/new — начать разговор с чистого листа."
        )

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        async with self.lock:
            self.engine.sessions.clear()
        await update.message.reply_text(
            "Начали с чистого листа. Всё сказанное сегодня сохранено — "
            "ночью я перечитаю день целиком, вместе с этой частью."
        )

    async def ping(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        prompt, channel = context.job.data
        await self._think_and_reply(prompt, self.owner_id, context, channel=channel)

    async def nightly_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ночью перечитать день, сложить конспект и начать разговор заново.

        Разговор не копится до упора в лимит: каждый день закрывается выжимкой,
        а преемственность держится журналом, а не длиной ленты сообщений.
        """
        async with self.lock:
            session_id = self.engine.sessions.load()
            yesterday = datetime.now(MOSCOW).date() - timedelta(days=1)
            try:
                await self.brain.pull()
                # Выжимку делаем всегда: день целиком лежит в архиве, даже если
                # ленту разговора сбросили через /new и session_id уже пуст.
                if await self.digester.make_day(session_id, yesterday):
                    self.engine.sessions.clear()

                if yesterday.weekday() == 6:  # воскресенье закрыло неделю
                    await self.digester.make_week(yesterday)
                if yesterday.day == 1:
                    await self.digester.make_month(yesterday)

                await self.brain.push(f"выжимка за {yesterday.isoformat()}")
            except Exception:
                log.exception("ночная выжимка сорвалась")

    # --- запуск ---

    def run(self) -> None:
        application = Application.builder().token(env("TELEGRAM_BOT_TOKEN", required=True)).build()

        application.add_handler(CommandHandler("start", self.on_start))
        # Telegram принимает только латиницу в командах — кириллические он отвергает
        application.add_handler(CommandHandler(["new", "reset"], self.on_reset))
        application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, self.on_voice))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))

        queue = application.job_queue
        morning = env("MORNING_TIME", "10:00")
        evening = env("EVENING_TIME", "20:00")
        queue.run_daily(self.ping, time=_parse(morning, MOSCOW), data=(MORNING_PROMPT, "morning"), name="утро")
        queue.run_daily(self.ping, time=_parse(evening, MOSCOW), data=(EVENING_PROMPT, "evening"), name="вечер")
        queue.run_daily(self.nightly_digest, time=_parse(env("DIGEST_TIME", "03:00"), MOSCOW), name="выжимка")

        log.info("коуч поднялся: пинги %s и %s по Москве, владелец %s", morning, evening, self.owner_id)
        # Вебхуки в РФ не годятся — Telegram их не достучится, работаем поллингом.
        application.run_polling(drop_pending_updates=True)


def _parse(value: str, tz: ZoneInfo) -> time:
    hour, _, minute = value.partition(":")
    return time(hour=int(hour), minute=int(minute or 0), tzinfo=tz)


if __name__ == "__main__":
    CoachBot().run()
