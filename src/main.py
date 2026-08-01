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

from . import agenda
from .archive import Archive
from .backstage import raise_task
from .brain import Brain
from .digest import Digester
from .engine import CoachEngine
from .memory_watch import MemoryWatch
from .prompts import followups, load as load_prompt, missing as missing_prompts
from .retry import retry_network
from .rhythms import describe, path_in, read
from . import settings as coach_settings
from .sessions import SessionStorage
from .startup_budget import check as check_budget
from .tidy_history import HistoryTidier
from .todoist_snapshot import TodoistSnapshot
from .voice import (
    DEFAULT_STT_MODEL,
    DEFAULT_STT_URL,
    OPENAI_STT_MODEL,
    OPENAI_STT_URL,
    Service,
    VoiceRecognizer,
)

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
# Куда кладём присланные картинки, чтобы коуч открыл их инструментом Read.
# В /tmp, а не в /brain и не в /state: файл нужен ровно на один ответ. В мозге
# он попал бы в git, в состоянии — в бэкап. После ответа файл удаляем.
PHOTOS_DIR = Path(os.environ.get("PHOTOS_DIR", "/tmp/coach-photos"))
# Что Read умеет показать как изображение. Присланное иным форматом (heic и
# прочая экзотика) не притворяемся, что видим, — честно говорим владельцу.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

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
        self.todoist_token = env("TODOIST_API_TOKEN", required=True)
        self.engine = CoachEngine(
            brain_dir=self.brain_dir,
            session_storage=SessionStorage(state_dir / "session_id"),
            system_prompt=self._system_prompt(),
            todoist_token=self.todoist_token,
            model=env("COACH_MODEL", "claude-fable-5"),
            effort=env("COACH_EFFORT", "medium"),
            calendar=self._calendar_config(),
            extra_dirs=[PHOTOS_DIR],
            dashboard={
                "brain_dir": self.brain_dir,
                "db_path": Path(env("ARCHIVE_DB", "/archive/coach.db")),
                "todoist_token": self.todoist_token,
                "send": self._send_document,
            },
        )
        # Заполняется в run(): инструмент дашборда шлёт файл через это приложение.
        self.app: Application | None = None
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        self.voice = self._voice_recognizer()
        self.archive = Archive(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        # Ритмы живут в мозге, а не в .env: их меняет Василий фразой в телеграме.
        self.rhythms, problems = read(self.brain_dir)
        if problems:
            log.error("ритмы в мозге сломаны, беру умолчания: %s", "; ".join(problems))
        # Настройки (модель и тумблеры кнопок) живут в мозге рядом с ритмами.
        # Файл читает код — значит код и заводит его, если человек ещё не завёл.
        coach_settings.ensure(self.brain_dir, datetime.now(MOSCOW).date().isoformat())
        values, beefs = coach_settings.read(self.brain_dir)
        if beefs:
            log.error("настройки в мозге сломаны, беру умолчания: %s", "; ".join(beefs))
        night_model = str(values["модель_ночной_работы"])
        self.digester = Digester(self.brain_dir, self.archive, model=night_model)
        self.tidier = HistoryTidier(self.brain_dir, model=night_model)
        self.memory_watch = MemoryWatch(self.brain_dir, model=night_model)
        # Несгораемая история дел: Todoist на Free помнит ~неделю, снимок помнит всё.
        self.snapshot = TodoistSnapshot(
            Path(env("ARCHIVE_DB", "/archive/coach.db")), self.todoist_token
        )
        self.broken_rhythms = "; ".join(problems)
        # Один разговор — значит одна очередь. Иначе две сессии подерутся за resume.
        self.lock = asyncio.Lock()

    @staticmethod
    def _voice_recognizer() -> VoiceRecognizer:
        """Основной сервис расшифровки — Groq (дешёвый), запасной — OpenAI.

        Если STT_API_KEY не задан (сервер со старым .env), основным становится
        OpenAI и запасного нет — бот всё равно поднимется и будет слышать голос.
        """
        proxy = env("HTTPS_PROXY") or None
        groq_key = env("STT_API_KEY")
        openai_key = env("OPENAI_API_KEY")
        if not (groq_key or openai_key):
            raise RuntimeError("нет ни STT_API_KEY, ни OPENAI_API_KEY — расшифровывать нечем")

        openai = (
            Service(
                name="OpenAI",
                api_key=openai_key,
                url=env("FALLBACK_STT_API_URL", OPENAI_STT_URL),
                model=env("FALLBACK_STT_MODEL", OPENAI_STT_MODEL),
            )
            if openai_key
            else None
        )
        if not groq_key:
            log.warning("STT_API_KEY не задан: расшифровка только через OpenAI, без запасного")
            return VoiceRecognizer(primary=openai, proxy=proxy)

        primary = Service(
            name="Groq",
            api_key=groq_key,
            url=env("STT_API_URL", DEFAULT_STT_URL),
            model=env("STT_MODEL", DEFAULT_STT_MODEL),
        )
        if openai is None:
            log.warning("OPENAI_API_KEY не задан: у расшифровки нет запасного сервиса")
        return VoiceRecognizer(primary=primary, fallback=openai, proxy=proxy)

    def _system_prompt(self) -> str:
        # Конституция живёт в плагине gtd-coach и приезжает смонтированным
        # клоном, а не копией в образе. Отсюда новый способ сломаться: если
        # клона на сервере нет, docker молча подставит пустую папку — бот
        # стартует и упадёт на чтении. Падать надо вслух и по адресу, иначе
        # причину искать полчаса.
        path = Path(env("PROMPT_FILE", "/plugin/prompts/coach.md"))
        if not path.is_file():
            raise FileNotFoundError(
                f"конституция коуча не найдена: {path}. Проверь, что клон плагина "
                f"есть на сервере и смонтирован (том /plugin в docker-compose.yml)"
            )
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
        self,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        channel: str = "text",
        archived_as: str | None = None,
    ) -> None:
        """Отдать реплику движку и ответить владельцу.

        `archived_as` нужен там, где движку уходит служебная обёртка, а в журнал
        должно лечь человеческое: у картинки в промпте путь к файлу, читать его
        в ночной выжимке незачем.
        """
        async with self.lock:
            await self.archive.add_message(
                "vasiliy", channel, archived_as or text, self.engine.sessions.load()
            )
            typing = asyncio.create_task(self._keep_typing(chat_id, context))
            try:
                await self.brain.pull()
                # Выжимки прошлых дней движок подставит сам, если разговор начинается заново.
                memory = await asyncio.to_thread(self.archive.recent_digests)
                # Сводку дел — к каждой реплике: коуч обязан знать картину дня всегда,
                # а не когда вспомнит сходить в Todoist.
                summary = await agenda.summary(self.todoist_token)
                await self._check_load(memory, summary)
                answer = await self.engine.ask(text, memory, summary)
                await self.brain.push(text[:60].replace("\n", " "))
            except Exception as error:  # доставляем боль владельцу, а не в лог-файл
                log.exception("сорвалось на ответе")
                answer = f"Сломался: {type(error).__name__}: {error}"
            finally:
                typing.cancel()
            await self.archive.add_message("coach", channel, answer, self.engine.sessions.load())

        # Коуч мог только что поменять ритмы по просьбе Василия — заметить это надо
        # сразу, а не через час: «пиши мне три раза в день» должно работать как фраза.
        await self._reread_rhythms(context, loud=True)

        for chunk in self._split(answer):
            await context.bot.send_message(chat_id=chat_id, text=chunk)

    async def _send_document(self, path: Path, caption: str = "") -> bool:
        """Отправить файл владельцу. Возвращает, дошёл ли.

        Библиотека это умела всегда, а бот — нет: до 31.07 у него был только
        send_message. Проверено grep'ом, не памятью.
        """
        if self.app is None:
            log.error("файл «%s» некуда слать: приложение ещё не поднято", path.name)
            return False
        try:
            with path.open("rb") as файл:
                await self.app.bot.send_document(
                    chat_id=self.owner_id, document=файл,
                    filename=path.name, caption=caption or None,
                )
        except Exception:  # телеграм упал, файл великоват, сеть — исход один
            log.exception("не смог отправить файл %s", path)
            return False
        return True

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

        # Путь голосового целиком идёт через Xray-мост: и скачивание из Telegram,
        # и расшифровка. Мост моргает — повторяем каждый шаг (см. retry.py).
        # Первые сбои проходят молча, но если возня затянулась — говорим об этом,
        # чтобы владелец не смотрел в тишину.
        warned = False

        async def warn(attempt: int, error: BaseException) -> None:
            nonlocal warned
            if warned or attempt < 3:
                return
            warned = True
            await context.bot.send_message(
                chat_id=chat_id, text="Связь шалит, пробую разобрать ещё раз — подожди."
            )

        async def switched(failed: str, taken: str) -> None:
            await context.bot.send_message(
                chat_id=chat_id, text=f"{failed} не отвечает — расшифровываю через {taken}."
            )

        typing = asyncio.create_task(self._keep_typing(chat_id, context))
        try:
            telegram_file = await retry_network(
                lambda: context.bot.get_file(source.file_id),
                what="получение ссылки на голосовое",
                on_retry=warn,
            )
            audio = bytes(
                await retry_network(
                    lambda: telegram_file.download_as_bytearray(),
                    what="скачивание голосового",
                    on_retry=warn,
                )
            )
            text = await self.voice.transcribe(audio, on_retry=warn, on_switch=switched)
        except Exception as error:
            log.exception("расшифровка не удалась")
            await context.bot.send_message(chat_id=chat_id, text=f"Не разобрал голос: {error}")
            return
        finally:
            typing.cancel()

        if not text:
            await context.bot.send_message(chat_id=chat_id, text="Тишина — ничего не разобрал.")
            return

        await context.bot.send_message(chat_id=chat_id, text=f"🎙 {text}")
        await self._think_and_reply(text, chat_id, context, channel="voice")

    @staticmethod
    def _image_source(message) -> tuple[object | None, str]:
        """Что прислали: сжатое фото или картинку файлом.

        У фото Telegram отдаёт лесенку размеров — берём последний, самый крупный:
        коучу нужны детали, а не превью.
        """
        if message.photo:
            return message.photo[-1], ".jpg"
        document = message.document
        if document and (document.mime_type or "").startswith("image/"):
            return document, Path(document.file_name or "").suffix.lower()
        return None, ""

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Картинка: кладём во временный файл и показываем коучу через Read.

        Зрение у бота не своё, а движка: инструмент Read у Claude Code открывает
        изображения и видит их. Поэтому работа телеграм-слоя простая — положить
        файл на диск, назвать путь и убрать за собой.

        Альбом из нескольких картинок Telegram присылает отдельными сообщениями,
        поэтому каждая разбирается своим ходом — связать их в одну мысль коуч
        может только по подписям.
        """
        if not self._mine(update):
            return
        chat_id = update.effective_chat.id
        message = update.message
        source, suffix = self._image_source(message)
        if source is None:
            return
        if suffix not in IMAGE_SUFFIXES:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Формат «{suffix or 'без расширения'}» я разглядеть не смогу. "
                "Пришли jpg, png, gif или webp — или обычным фото, без файла.",
            )
            return

        # Путь картинки, как и путь голосового, идёт через Xray-мост: мост моргает —
        # повторяем шаг (см. retry.py). Молчим, пока возня короткая.
        warned = False

        async def warn(attempt: int, error: BaseException) -> None:
            nonlocal warned
            if warned or attempt < 3:
                return
            warned = True
            await context.bot.send_message(
                chat_id=chat_id, text="Связь шалит, пробую забрать картинку ещё раз — подожди."
            )

        typing = asyncio.create_task(self._keep_typing(chat_id, context))
        try:
            telegram_file = await retry_network(
                lambda: context.bot.get_file(source.file_id),
                what="получение ссылки на картинку",
                on_retry=warn,
            )
            picture = bytes(
                await retry_network(
                    lambda: telegram_file.download_as_bytearray(),
                    what="скачивание картинки",
                    on_retry=warn,
                )
            )
        except Exception as error:
            log.exception("картинка не скачалась")
            await context.bot.send_message(chat_id=chat_id, text=f"Не забрал картинку: {error}")
            return
        finally:
            typing.cancel()

        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        path = PHOTOS_DIR / f"{update.update_id}{suffix}"
        await asyncio.to_thread(path.write_bytes, picture)

        caption = (message.caption or "").strip()
        prompt = (
            f"Василий прислал картинку. Открой её инструментом Read по пути {path} — "
            "это изображение, ты его увидишь.\n"
            + (f"Подписал так: {caption}\n" if caption else "")
            + "Ответь по тому, что на картинке, и по подписи, если она есть."
        )
        try:
            await self._think_and_reply(
                prompt,
                chat_id,
                context,
                channel="photo",
                archived_as=f"[картинка] {caption}" if caption else "[картинка]",
            )
        finally:
            # Одноразовое сносим сразу: картинка нужна была ровно на этот ответ.
            path.unlink(missing_ok=True)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сбой в обработке — в лог по-человечески, а не строкой «обработчиков нет».

        Обработчик ничего не чинит: 30.07.2026 разбор показал, что цикл поллинга
        в python-telegram-bot 22.8 настроен повторять попытки бесконечно и от
        сетевой ошибки не умирает, — значит регистрация обработчика не была
        лечением, вопреки записи в разборе от 25.07. Он нужен ради видимости:
        без него библиотека печатает трейсбек с формулировкой, которая уводит
        разбор не туда. Живучесть ушей держит внешний сторож, а не эта функция.
        """
        error = context.error
        log.error("сбой в обработке: %s: %s", type(error).__name__, error, exc_info=error)

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
        # В задании лежит ИМЯ промпта, а не текст: файл читается в момент пинга,
        # значит правка текста в плагине доезжает перезапуском, а не пересборкой.
        name, channel = context.job.data
        now = datetime.now(MOSCOW)
        # Первого числа утренний чек-ин уступает место стратсессии: месяц
        # закрылся ночью, и разговор в этот день начинается с итога, а не
        # с обещания дня. Двух сообщений подряд не шлём — это один разговор.
        if name == "чекин-утро" and now.day == 1:
            # Подводим ЗАКРЫВШИЙСЯ месяц, а не начавшийся: первого августа
            # итог пишется за июль. Отсюда шаг на день назад.
            closed = (now.date() - timedelta(days=1)).strftime("%Y-%m")
            prompt = load_prompt("месячный-итог").format(month=closed)
            channel = "monthly"
        else:
            prompt = load_prompt(name).text
        sent_at = now.isoformat(timespec="seconds")
        await self._think_and_reply(prompt, self.owner_id, context, channel=channel)
        self._schedule_followup(context, sent_at, attempt=1)

    def _schedule_followup(self, context: ContextTypes.DEFAULT_TYPE, sent_at: str, attempt: int) -> None:
        """Завести следующую попытку достучаться, если она ещё в запасе."""
        if attempt > len(followups()):
            return
        context.job_queue.run_once(
            self.follow_up,
            when=timedelta(minutes=self.rhythms["дожим_минут"]),
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
        if now.hour >= self.rhythms["тихий_час"] or now.hour < 8:
            log.info("дожим %s отменён: время тихое (%s)", attempt, now.strftime("%H:%M"))
            return
        attempts = followups()
        if attempt > len(attempts):  # промпт убрали из плагина, пока дожим ждал в очереди
            return
        await self._think_and_reply(
            attempts[attempt - 1].text, self.owner_id, context, channel="nudge"
        )
        self._schedule_followup(context, sent_at, attempt + 1)

    async def nightly_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ночью перечитать день, сложить конспект и начать разговор заново.

        Разговор не копится до упора в лимит: каждый день закрывается выжимкой,
        а преемственность держится журналом, а не длиной ленты сообщений.
        """
        async with self.lock:
            session_id = self.engine.sessions.load()
            today = datetime.now(MOSCOW).date()
            yesterday = today - timedelta(days=1)
            try:
                await self.brain.pull()
                # Выжимку делаем всегда: день целиком лежит в архиве, даже если
                # ленту разговора сбросили через /new и session_id уже пуст.
                if await self.digester.make_day(session_id, yesterday):
                    self.engine.sessions.clear()

                # Второй вопрос к тому же дню — уже про саму память: что пора
                # записать и где она сегодня подвела. Просьба в тексте конституции
                # («увидел паттерн — допиши») не срабатывала; будильник срабатывает.
                found = await self.memory_watch.run(
                    yesterday, self.digester.transcript_of(yesterday)
                )
                # Найденное молча в копилке — свалка. Дёргаем за рукав задачей
                # с датой: одна на копилку, не на каждую находку.
                if found.get("факты") or found.get("выводы"):
                    await raise_task(
                        self.todoist_token, "предложения",
                        f"Ночь {yesterday.isoformat()}: фактов {found['факты']}, "
                        f"выводов {found['выводы']}. Выводы записываются только "
                        f"после подтверждения Василием.",
                    )
                if found.get("промахи"):
                    await raise_task(
                        self.todoist_token, "промахи",
                        f"Ночь {yesterday.isoformat()}: промахов {found['промахи']}.",
                    )

                # Снимок дел — прежде укрупнений: он должен успеть сохранить то,
                # что человек может удалить завтра. Ноль токенов, чистый код.
                await self.snapshot.run(today)

                # Укрупняем только законченные периоды и только по календарю.
                if yesterday.weekday() == 6:  # воскресенье закрыло неделю
                    await self.digester.make_week(yesterday)
                if today.day == 1:  # первое число: вчера закрыло месяц
                    await self.digester.make_month(yesterday)
                # Журнал держим в том же окне, что читает коуч, — вместе с адресами.
                self.digester.rotate()

                if not await self.brain.push(f"выжимка за {yesterday.isoformat()}"):
                    # push вернул False и когда менять было нечего, и когда он упал.
                    # Различаем по логу; задачу поднимаем только если правки были.
                    if await self.brain.dirty():
                        await raise_task(
                            self.todoist_token, "синхронизация",
                            f"Ночь {yesterday.isoformat()}: изменения в мозге есть, "
                            f"а на GitHub они не уехали.",
                        )

                # История дня подписывается по-человечески той же моделью,
                # что делала выжимку — сторож на маке для этого слишком туп и быстр.
                await self.tidier.tidy(yesterday)
            except Exception:
                log.exception("ночная выжимка сорвалась")


    async def _check_load(self, memory: str, summary: str) -> None:
        """Сверить то, что реально кладём в контекст, с паспортом памяти.

        Считаем только когда память подставляется — то есть в первый запрос новой
        сессии. В середине разговора выжимки не грузятся, и «сумма» была бы
        не той величиной, о которой говорит паспорт.
        """
        if not memory or self.engine.sessions.load():
            return
        beef = check_budget(self.brain_dir, {
            "конституция коуча (роль, тон, что делать с делами)": len(
                self.engine.system_prompt.encode("utf-8")
            ),
            "окно выжимок — 15 кусков всегда (7 дней + 5 недель + 3 месяца)": len(
                memory.encode("utf-8")
            ),
            "сводка дел — агрегат Todoist к каждой реплике": len(summary.encode("utf-8")),
            # Описания кнопок кладёт в контекст движок, а не наш код, — и ровно
            # поэтому они годами не попадали в паспорт. Считает тот, кто грузит;
            # здесь мы просто спрашиваем у движка, во что обошёлся его набор.
            "описания кнопок инструментов": await self.engine.tools_weight(),
        })
        if beef:
            log.warning("стартовая загрузка разошлась с паспортом: %s", beef)
            # Сигнализация без реакции — декорация. Лампочка горит → задача с датой.
            await raise_task(self.todoist_token, "потолок", beef)

    # --- ритмы ---

    # Ключ ритма → (имя будильника, имя промпта в плагине, канал архива).
    ALARMS = {
        "утро": ("утро", "чекин-утро", "morning"),
        "день": ("день", "чекин-день", "midday"),
        "вечер": ("вечер", "чекин-вечер", "evening"),
    }

    def _hang_alarms(self, queue) -> None:
        """Перевесить будильники по нынешним ритмам. Старые снимаются по имени."""
        for name in list(self.ALARMS) + ["выжимка"]:
            for job in queue.get_jobs_by_name(name):
                job.schedule_removal()
        for key, (name, prompt_name, channel) in self.ALARMS.items():
            queue.run_daily(
                self.ping, time=_parse(self.rhythms[key], MOSCOW),
                data=(prompt_name, channel), name=name,
            )
        queue.run_daily(
            self.nightly_digest, time=_parse(self.rhythms["ночная_выжимка"], MOSCOW), name="выжимка"
        )

    async def _reread_rhythms(self, context: ContextTypes.DEFAULT_TYPE, loud: bool) -> None:
        """Перечитать файл ритмов и, если он изменился, перевесить будильники.

        `loud` — говорить ли Василию. После его собственной реплики говорим: он
        только что попросил поменять расписание и должен увидеть, что вышло.
        Из часового сторожа молчим про «всё по-прежнему», но про поломку — всегда.
        """
        fresh, problems = read(self.brain_dir)
        if problems:
            beef = "; ".join(problems)
            log.error("ритмы в мозге сломаны: %s", beef)
            if self.broken_rhythms != beef:
                self.broken_rhythms = beef
                await context.bot.send_message(
                    chat_id=self.owner_id,
                    text=(f"Файл ритмов не читается, живу по прежнему расписанию.\n{beef}\n\n"
                          f"Адрес: {path_in(self.brain_dir)}"),
                )
                # Сообщение уползёт вверх и забудется — дублируем задачей с датой.
                await raise_task(
                    self.todoist_token, "ритмы", f"{beef}\nАдрес: {path_in(self.brain_dir)}"
                )
            return
        self.broken_rhythms = ""
        if fresh == self.rhythms:
            return
        changes = describe(fresh, self.rhythms)
        self.rhythms = fresh
        self._hang_alarms(context.job_queue)
        log.info("ритмы перечитаны: %s", changes)
        if loud:
            await context.bot.send_message(
                chat_id=self.owner_id, text=f"Расписание переставил: {changes}."
            )

    async def watch_rhythms(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reread_rhythms(context, loud=False)

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
        # Тексты поведения живут в плагине. Нехватка обязана вскрыться здесь,
        # одной понятной строкой, а не ночью в 03:00 внутри ночного прогона.
        gone = missing_prompts()
        if gone:
            raise SystemExit(
                "В плагине нет промптов: " + ", ".join(gone) +
                ". Проверь, что клон плагина на сервере свежий и том /plugin смонтирован."
            )
        application = Application.builder().token(env("TELEGRAM_BOT_TOKEN", required=True)).build()
        self.app = application  # через него инструмент дашборда шлёт файл

        application.add_handler(CommandHandler("start", self.on_start))
        # Telegram принимает только латиницу в командах — кириллические он отвергает
        application.add_handler(CommandHandler(["new", "reset"], self.on_reset))
        application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, self.on_voice))
        # Картинка приходит либо сжатым фото, либо файлом — ловим оба входа.
        application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.on_photo))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        application.add_error_handler(self.on_error)

        queue = application.job_queue
        self._hang_alarms(queue)
        queue.run_repeating(self.heartbeat, interval=60, first=1, name="пульс")
        # Сторож на случай, когда файл ритмов приехал со стороны — например, git pull
        # притащил правку с другой машины. Разговор такую правку не заметит.
        queue.run_repeating(self.watch_rhythms, interval=3600, first=3600, name="сторож ритмов")

        log.info("коуч поднялся: %s, владелец %s", describe(self.rhythms), self.owner_id)
        # Вебхуки в РФ не годятся — Telegram их не достучится, работаем поллингом.
        # Накопленное НЕ выбрасываем: бота перезапускает сторож «бот оглох» именно
        # тогда, когда очередь не разбирается, — и флаг «выбросить» уничтожал бы
        # ровно те сообщения, ради которых чинили (ожог 25.07 и 30.07.2026).
        # Цена: после долгого простоя коуч ответит на всё, что накопилось.
        application.run_polling(drop_pending_updates=False)


def _parse(value: str, tz: ZoneInfo) -> time:
    hour, _, minute = value.partition(":")
    return time(hour=int(hour), minute=int(minute or 0), tzinfo=tz)


if __name__ == "__main__":
    CoachBot().run()
