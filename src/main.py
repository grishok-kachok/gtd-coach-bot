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
from .tidy_history import HistoryTidier
from .voice import VoiceRecognizer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("coach")

TELEGRAM_LIMIT = 4000
MOSCOW = ZoneInfo("Europe/Moscow")
# Отметка живости для docker healthcheck. В /tmp, а не в /state: это одноразовое
# состояние процесса, ему нечего делать в бэкапе вместе с закладкой разговора.
HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/coach-heartbeat"))

MORNING_PROMPT = (
    "Наступило утро — время утреннего чек-ина. Перед тем как писать: загляни в "
    "память/состояние/фокус.md, в Todoist (сегодня + просроченное) и во вчерашние "
    "выполненные задачи. Дальше действуй по правилам раздела «Когда пишешь первым»: "
    "главное дело дня и почему именно сегодня, вчерашние победы одной строкой (если "
    "были), светофор только по действительно критичным задачам — и возьми с Василия "
    "обещание дня. Сочини сообщение заново, живым языком, без шаблона."
)

MIDDAY_PROMPT = (
    "Середина дня — чек-ин внешней ответственности, не напоминалка. Вспомни, что "
    "Василий пообещал утром (это выше в разговоре), загляни в Todoist — что уже "
    "закрыто. Спроси конкретно про обещанное: как продвигается, что мешает, нужна ли "
    "помощь — один вопрос. Утром не ответил — начни с этого, без укора. Видно, что "
    "день идёт хорошо — скажи это одной фразой и не мешай работать."
)

EVENING_PROMPT = (
    "Наступил вечер — время подвести день. Сверь утреннее обещание с фактом. Спроси, "
    "что сделал и что перенести; сделанное закрой в Todoist, несделанное перенеси "
    "осознанно. Отметь победы конкретно, с фактами. Увидел паттерн дня — скажи как "
    "зеркало. Закончи ощущением «всё поймано»: одной строкой подтверди, что всё "
    "записано и голову можно освободить."
)

# Дожим: Василий не отозвался на чек-ин. Внешняя ответственность не работает, если
# от неё можно молча отвернуться, — но и долбить нельзя, иначе пролистывать начнут
# всё подряд. Отсюда три попытки, каждая другой по характеру, и тишина после отбоя.
FOLLOWUP_PROMPTS = (
    "Василий не ответил на прошлый чек-ин — прошло полчаса. Достучись: коротко, "
    "тепло, с юмором, без укора и без повторения того, что уже написал. Одна-две "
    "строки, один вопрос. Смысл: «ты тут? я на связи».",
    "Василий молчит второй раз подряд — это последняя попытка достучаться сегодня, "
    "больше сегодня по этому поводу не пишешь. Смени подход: не спрашивай «как дела», "
    "а поставь что-то на кон или назови вслух то, что видишь (например, что задача "
    "стоит на месте второй день). Коротко и цепко, чтобы захотелось ответить. "
    "В конце дай понять, что отстаёшь и ждёшь его хода."
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
            calendar=self._calendar_config(),
        )
        self.voice = VoiceRecognizer(
            api_key=env("OPENAI_API_KEY", required=True),
            proxy=env("HTTPS_PROXY") or None,
        )
        self.archive = Archive(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        self.memory_days = int(env("MEMORY_DAYS", "30"))
        self.followup_minutes = int(env("FOLLOWUP_MINUTES", "30"))
        self.quiet_hour = int(env("QUIET_HOUR", "23"))  # после этого часа не дожимаем
        self.digester = Digester(self.brain_dir, self.archive, model=env("DIGEST_MODEL", "claude-fable-5"))
        self.tidier = HistoryTidier(self.brain_dir, model=env("DIGEST_MODEL", "claude-fable-5"))
        # Один разговор — значит одна очередь. Иначе две сессии подерутся за resume.
        self.lock = asyncio.Lock()

    def _system_prompt(self) -> str:
        path = Path(env("PROMPT_FILE", "/app/prompts/coach.md"))
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _calendar_config() -> dict | None:
        """Google-календарь подключаем, только если заданы все три секрета.

        Часовой пояс настраиваемый: с сентября владелец переезжает на Бали —
        поменяется одна переменная COACH_TZ. Выход к Google — через тот же
        Xray-мост, что у Whisper (Google в РФ блокируется)."""
        client_id = env("GOOGLE_CLIENT_ID")
        client_secret = env("GOOGLE_CLIENT_SECRET")
        refresh_token = env("GOOGLE_REFRESH_TOKEN")
        if not (client_id and client_secret and refresh_token):
            log.warning("Google-календарь не подключён: не заданы GOOGLE_* переменные")
            return None
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "calendar_id": env("GOOGLE_CALENDAR_ID", "primary"),
            "tz": env("COACH_TZ", "Europe/Moscow"),
            "proxy": env("HTTPS_PROXY") or None,
        }

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
            "Пингую утром в 10:00, днём в 14:30 и вечером в 20:00.\n"
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
        sent_at = datetime.now(MOSCOW).isoformat(timespec="seconds")
        await self._think_and_reply(prompt, self.owner_id, context, channel=channel)
        self._schedule_followup(context, sent_at, attempt=1)

    def _schedule_followup(self, context: ContextTypes.DEFAULT_TYPE, sent_at: str, attempt: int) -> None:
        """Завести следующую попытку достучаться, если она ещё в запасе."""
        if attempt > len(FOLLOWUP_PROMPTS):
            return
        context.job_queue.run_once(
            self.follow_up,
            when=timedelta(minutes=self.followup_minutes),
            data=(sent_at, attempt),
            name=f"дожим-{attempt}",
        )

    async def follow_up(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Василий не отозвался на чек-ин — попробовать ещё раз, иначе отступить.

        Проверяем факт по архиву, а не по флагу в памяти: бот мог быть перезапущен,
        а ответ мог прийти любым каналом.
        """
        sent_at, attempt = context.job.data
        if await asyncio.to_thread(self.archive.answered_since, sent_at):
            log.info("дожим %s не нужен: Василий отозвался", attempt)
            return
        now = datetime.now(MOSCOW)
        if now.hour >= self.quiet_hour or now.hour < 8:
            log.info("дожим %s отменён: время тихое (%s)", attempt, now.strftime("%H:%M"))
            return
        await self._think_and_reply(
            FOLLOWUP_PROMPTS[attempt - 1], self.owner_id, context, channel="nudge"
        )
        self._schedule_followup(context, sent_at, attempt + 1)

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

                # История дня подписывается по-человечески той же моделью,
                # что делала выжимку — сторож на маке для этого слишком туп и быстр.
                await self.tidier.tidy(yesterday)
            except Exception:
                log.exception("ночная выжимка сорвалась")

    # --- пульс ---

    async def heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Отметка «рабочий цикл жив» для docker healthcheck (src/healthcheck.py).

        Задача крутится в том же asyncio-цикле, что и поллинг Telegram: встанет
        цикл — перестанет обновляться и файл. Наружу бот ничего не слушает,
        поэтому другого дешёвого признака живости у него нет.
        """
        try:
            HEARTBEAT_FILE.write_text(datetime.now(MOSCOW).isoformat(timespec="seconds"))
        except OSError:
            log.exception("не смог записать пульс в %s", HEARTBEAT_FILE)

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
        midday = env("MIDDAY_TIME", "14:30")
        evening = env("EVENING_TIME", "20:00")
        queue.run_daily(self.ping, time=_parse(morning, MOSCOW), data=(MORNING_PROMPT, "morning"), name="утро")
        queue.run_daily(self.ping, time=_parse(midday, MOSCOW), data=(MIDDAY_PROMPT, "midday"), name="день")
        queue.run_daily(self.ping, time=_parse(evening, MOSCOW), data=(EVENING_PROMPT, "evening"), name="вечер")
        queue.run_daily(self.nightly_digest, time=_parse(env("DIGEST_TIME", "03:00"), MOSCOW), name="выжимка")
        queue.run_repeating(self.heartbeat, interval=60, first=1, name="пульс")

        log.info("коуч поднялся: пинги %s, %s и %s по Москве, владелец %s", morning, midday, evening, self.owner_id)
        # Вебхуки в РФ не годятся — Telegram их не достучится, работаем поллингом.
        application.run_polling(drop_pending_updates=True)


def _parse(value: str, tz: ZoneInfo) -> time:
    hour, _, minute = value.partition(":")
    return time(hour=int(hour), minute=int(minute or 0), tzinfo=tz)


if __name__ == "__main__":
    CoachBot().run()
