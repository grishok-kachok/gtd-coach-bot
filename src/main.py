"""Телеграм-канал коуча: приём голоса и текста, два пинга в день.

Здесь только доставка. Всё, что думает, живёт в engine.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, Update
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
from .comments import Канал, build_undo_server
from .context_cost import ContextCost
from . import detectors
from .digest import Digester
from .engine import CoachEngine
from .memory_watch import MemoryWatch
from . import modes as режимы_модуль
from . import profile
from .promise import PromiseWatch
from .prompts import followups, load as load_prompt, missing as missing_prompts
from .recall import build_recall_server
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

# Клавиатура — не новая дверь, а печатающая за человека рука. Каждая кнопка
# отправляет в чат ровно ту фразу, которую можно сказать голосом; выключишь
# клавиатуру — всё продолжит работать словами. Поэтому и reply, а не inline:
# inline-кнопка шлёт скрытый код, которого в разговоре не видно, и тогда
# кнопка становится второй дверью со своей логикой.
КЛАВИАТУРА = ReplyKeyboardMarkup(
    [["Что сегодня?", "Что по плану на неделю?"],
     ["Проведём недельный обзор", "Покажи дашборд"],
     ["Переключись в полный режим", "Вернись в рабочий режим"]],
    resize_keyboard=True,
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
        self.todoist_token = env("TODOIST_API_TOKEN", required=True)
        # Заполняется в run(): через это приложение уходят файлы и жалобы канала.
        self.app: Application | None = None
        self.archive = Archive(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        # Состав режимов приезжает из плагина. Сломан или отсутствует — падаем
        # здесь, одной понятной строкой: без состава неизвестно, что класть
        # коучу в голову, а зашитый в питон запасной был бы вторым домом.
        self.modes = режимы_модуль.load()
        # Цена контекста в токенах — факт от модели, а не пересчёт байтов.
        self.cost = ContextCost(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        # Вторая дверь к коучу — комментарии в карточках задач. Заводится до
        # движка: движку нужна её кнопка отката.
        self.comments = Канал(
            token=self.todoist_token,
            db_path=Path(env("ARCHIVE_DB", "/archive/coach.db")),
            archive=self.archive,
            model=env("COACH_MODEL", "claude-fable-5"),
            сказать=self._сказать_владельцу,
            mode=self.modes[режимы_модуль.ФОНОВЫЙ],
            cost=self.cost,
        )
        self.engine = CoachEngine(
            brain_dir=self.brain_dir,
            session_storage=SessionStorage(state_dir / "session_id"),
            modes=self.modes,
            cost=self.cost,
            recall=build_recall_server(Path(env("ARCHIVE_DB", "/archive/coach.db"))),
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
            undo=build_undo_server(self.comments),
        )
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        self.voice = self._voice_recognizer()
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
        # Вся фоновая работа идёт в фоновом режиме, и ставит его код: человек
        # ночную выжимку не заказывает и её головой не пользуется.
        фон = self.modes[режимы_модуль.ФОНОВЫЙ]
        self.digester = Digester(self.brain_dir, self.archive, model=night_model,
                                 mode=фон, cost=self.cost)
        self.tidier = HistoryTidier(self.brain_dir, model=night_model,
                                    mode=фон, cost=self.cost)
        self.memory_watch = MemoryWatch(self.brain_dir, model=night_model,
                                        mode=фон, cost=self.cost)
        # Несгораемая история дел: Todoist на Free помнит ~неделю, снимок помнит всё.
        self.snapshot = TodoistSnapshot(
            Path(env("ARCHIVE_DB", "/archive/coach.db")), self.todoist_token
        )
        # Обещание дня разбирается по тому же архиву и ложится в ту же базу:
        # дом цифр — база, дом смысла — журнал.
        self.promises = PromiseWatch(self.snapshot.db_path, model=night_model)
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
            mode = self.engine.режим()
            try:
                await self.brain.pull()
                # Рюкзак собирается ПОД РЕЖИМ. Выжимки движок подставит сам, если
                # разговор начинается заново; сколько их — решает режим.
                mode = self.engine.режим()
                memory = await asyncio.to_thread(self.archive.recent_digests, mode.окно)
                # Стратегия и профиль едут тем же заходом. Раньше это была просьба
                # в конституции «читай в начале разговора» — по десяти последним
                # сессиям она сработала в одной.
                strategy = await asyncio.to_thread(self.engine.файлы_стратегии, mode)
                # Сводку дел — к каждой реплике: коуч обязан знать картину дня всегда,
                # а не когда вспомнит сходить в Todoist.
                summary = await agenda.summary(self.todoist_token)
                answer = await self.engine.ask(text, memory, summary, strategy, channel)
                await self._check_load(mode, channel, summary, text)
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
        # И режим тоже: «переключись в полный» — такая же фраза, как «пиши три
        # раза в день». Разговор при этом закроется и откроется заново — это
        # делает сам движок перед следующей репликой.
        await self._заметить_режим(context, mode)

        куски = self._split(answer)
        for номер, chunk in enumerate(куски, 1):
            # Значок режима — в самом конце последнего куска. Один символ,
            # читать не мешает, всегда на виду. Заголовок «Режим: рабочий»
            # перед каждым ответом надоел бы на третий день.
            хвост = f"\n\n{self.engine.режим().значок}" if номер == len(куски) else ""
            await context.bot.send_message(
                chat_id=chat_id, text=chunk + хвост, reply_markup=КЛАВИАТУРА,
            )

    async def _заметить_режим(self, context: ContextTypes.DEFAULT_TYPE, было) -> None:
        """Сказать вслух, если режим переключился.

        Переключает его сам коуч, правя `настройки.md` по просьбе Василия, —
        то есть машинная правка, и по правилу проекта она обязана быть видимой.
        Молча сменившийся режим означал бы, что человек не знает, с какой
        головой он сейчас разговаривает.
        """
        стало = self.engine.режим()
        if стало.имя == было.имя:
            return
        log.info("режим переключён: %s → %s", было.имя, стало.имя)
        await context.bot.send_message(
            chat_id=self.owner_id,
            text=(f"{стало.значок} Переключил режим: {было.имя} → {стало.имя} "
                  f"({стало.повод}).\nСледующая реплика начнёт новый разговор — "
                  f"память подкладывается только в его начало."),
            reply_markup=КЛАВИАТУРА,
        )

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

    async def _сказать_владельцу(self, текст: str) -> None:
        """Сказать что-то в телеграм не в ответ на реплику, а от себя.

        Нужна фоновым сторожам: канал комментариев так жалуется на потолок.
        Приложение поднимается позже конструктора, поэтому проверяем.
        """
        if self.app is None:
            log.error("некому сказать: приложение ещё не поднято (%s)", текст[:60])
            return
        try:
            await self.app.bot.send_message(chat_id=self.owner_id, text=текст)
        except Exception:
            log.exception("не смог написать владельцу")

    async def watch_comments(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Опрос комментариев в Todoist — вторая дверь к коучу.

        Пустой цикл стоит 215 байт и один HTTP-запрос: модель просыпается
        только на обращение по имени. Молчим в логах о пустоте — 480 строк
        «ничего нет» в сутки прячут настоящую поломку.
        """
        # Модель канала берём из настроек мозга, как и модель разговора:
        # «переключись на Opus» обязано менять и работника комментариев.
        values, beefs = coach_settings.read(self.brain_dir)
        if values and not beefs:
            self.comments.model = str(values["модель_разговора"])
        try:
            итог = await self.comments.шаг()
        except Exception:
            log.exception("канал комментариев сорвался")
            return
        if итог["обращений"] or итог["новых"]:
            log.info(
                "канал комментариев: новых %d, обращений %d, разобрано %d",
                итог["новых"], итог["обращений"], итог["сделано"],
            )

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
        режим = self.engine.режим()
        await update.message.reply_text(
            "Я на связи. Наговаривай или пиши — разберём дела.\n"
            "Пингую утром в 10:00, днём в 14:30 и вечером в 20:00.\n"
            f"Сейчас режим {режим.значок} {режим.имя} — {режим.повод}.\n"
            "Кнопки внизу просто печатают за тебя: то же самое можно сказать голосом.\n"
            "/new — начать разговор с чистого листа.",
            reply_markup=КЛАВИАТУРА,
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
        # Утро понедельника и первое число — поводы для обзора. Раньше первого
        # числа код сам ЗАПУСКАЛ месячный итог; теперь коуч его ПРЕДЛАГАЕТ.
        # Разница не в вежливости: стратсессия по звонку не случается, а вот
        # переключение режима без согласия человека — случается, и он получает
        # дорогую голову там, где просил «что сегодня».
        повод = self._повод_обзора(now)
        if name == "чекин-утро" and повод:
            prompt = load_prompt("предложение-обзора").format(**повод)
            channel = повод["канал"]
        else:
            prompt = load_prompt(name).text
            if name == "чекин-утро":
                # Бриф считается ЗДЕСЬ, а не ночью. Ночная проза состарилась бы
                # за семь часов, а второй вызов модели ничего бы не добавил:
                # утренний чек-ин и так идёт через модель с полным контекстом.
                # Код даёт цифры, коуч решает, что из них главное.
                prompt = self._with_brief(prompt, await self._обход())
        sent_at = now.isoformat(timespec="seconds")
        await self._think_and_reply(prompt, self.owner_id, context, channel=channel)
        self._schedule_followup(context, sent_at, attempt=1)

    @staticmethod
    def _повод_обзора(now) -> dict | None:
        """Есть ли сегодня повод предложить обзор, и какой.

        Порядок проверки от крупного к мелкому: первое января — это ещё
        и первое число, и, случись оно понедельником, три повода разом.
        Предлагаем **один**, самый крупный: три предложения подряд читаются
        как три задачи, хотя это один разговор.
        """
        if now.month == 1 and now.day == 1:
            return {"повод": f"{now.year - 1} год", "что": "годовую стратсессию",
                    "режим": "годовой", "навык": "месячный-итог", "канал": "yearly"}
        if now.day == 1:
            # Подводим ЗАКРЫВШИЙСЯ месяц, а не начавшийся: первого августа
            # итог пишется за июль. Отсюда шаг на день назад.
            закрылся = (now.date() - timedelta(days=1)).strftime("%Y-%m")
            return {"повод": f"месяц {закрылся}", "что": "месячный итог",
                    "режим": "полный", "навык": "месячный-итог", "канал": "monthly"}
        if now.weekday() == 0:
            return {"повод": "неделя", "что": "недельный обзор",
                    "режим": "полный", "навык": "недельный-обзор", "канал": "weekly"}
        return None

    async def _обход(self) -> detectors.Свод:
        """Обход завалов. Упал — пустой свод и жалоба в лог: чек-ин важнее."""
        try:
            return await detectors.обойти(
                self.todoist_token, self.snapshot.db_path, self._calendar_config()
            )
        except Exception:
            log.exception("обход завалов сорвался")
            return detectors.Свод()

    @staticmethod
    def _with_brief(prompt: str, свод: detectors.Свод) -> str:
        """Подложить свод к чек-ину. Пусто — не подкладываем ничего.

        Подстановка, а не просьба «посмотри завалы» в тексте: просьба
        срабатывает, если модель не отвлеклась, — на этом в июле протекло
        знание. Пустой свод не превращается в строку «всё в норме»: коуч
        не должен отчитываться о проверке, которая ничего не нашла.
        """
        текст = свод.текст()
        return f"{prompt}\n\n{текст}" if текст else prompt

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

                # Обещание дня против факта. Разбор идёт ПОСЛЕ снимка: список
                # закрытых за вчера должен быть уже в базе, иначе сверять не с чем.
                await self.promises.run(yesterday, self.digester.transcript_of(yesterday))

                # Обход завалов ночью нужен не ради брифа (тот считается утром),
                # а ради счёта суток: лампочка, горящая третьи сутки подряд, —
                # это уже долг, и он дёргает за рукав задачей. Прибор без
                # реакции — декорация.
                свод = await self._обход()
                висит = await asyncio.to_thread(
                    detectors.запомнить, self.snapshot.db_path, свод, today
                )
                if висит:
                    # Один вызов на всё созревшее, а не по вызову на отклонение.
                    # Правило «одна задача на копилку» соблюдалось и так — вторая
                    # находка дописывает комментарий к первой, — но шесть
                    # комментариев подряд читаются как шесть проблем, хотя это
                    # один разговор. Поймано проверкой этапа: в первое же
                    # созревание их оказалось ровно шесть.
                    await raise_task(
                        self.todoist_token, "завал",
                        f"{today.isoformat()}, держится третьи сутки:\n"
                        + "\n".join(f"- {о.строка}" for о in висит),
                    )

                # Профиль: показания приборов про самого человека. Состояние
                # перезаписываем каждую ночь, строку в журнал — раз в неделю,
                # в ту же ночь, что собирается недельная выжимка. Ряд, а не
                # одна оценка: важно не «сегодня столько», а «поехало».
                показатели = await asyncio.to_thread(
                    profile.собрать, self.snapshot.db_path, self.archive.path, today
                )
                if показатели:
                    await asyncio.to_thread(
                        profile.записать, self.brain_dir, показатели, today,
                        yesterday.weekday() == 6,
                    )

                # Укрупняем только законченные периоды и только по календарю.
                if yesterday.weekday() == 6:  # воскресенье закрыло неделю
                    await self.digester.make_week(yesterday)
                if today.day == 1:  # первое число: вчера закрыло месяц
                    await self.digester.make_month(yesterday)
                # Журнал держим в том же окне, что читает коуч, — вместе с адресами.
                self.digester.rotate()

                # Обзоры. Агент стратсессию сам не запускает — но задача с датой
                # ставится сама, потому что предложение в чате уползает вверх
                # за полдня. Ровно правило «фоновая работа ставит задачу с датой».
                await self._поставить_обзоры(today)

                # Оглавление памяти. Валидатор ловит сироту и битую ссылку, но
                # срабатывает при записи; запись могла пройти мимо хука. Раз
                # в сутки проверяем ещё раз — расхождение обязано находиться
                # не тогда, когда о него споткнутся.
                await self._проверить_оглавление()

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


    async def _поставить_обзоры(self, today) -> None:
        """Задачи на обзоры — понедельник, первое число, первое января.

        Три отдельных повода, а не один список: у каждого своя дата и свой
        разговор. Вторая задача не заводится — `raise_task` допишет комментарий
        к уже стоящей, это его штатное поведение.
        """
        поводы = []
        if today.weekday() == 0:
            поводы.append(("недельный обзор", f"Неделя началась {today.isoformat()}."))
        if today.day == 1:
            закрывшийся = (today - timedelta(days=1)).strftime("%Y-%m")
            поводы.append(("месячный итог", f"Закрылся {закрывшийся}."))
        if today.month == 1 and today.day == 1:
            поводы.append(("годовая стратсессия", f"Закрылся {today.year - 1} год."))
        for повод, заметка in поводы:
            исход = await raise_task(self.todoist_token, повод, заметка)
            log.info("задача на «%s»: %s", повод, исход or "не доехала до Todoist")

    async def _проверить_оглавление(self) -> None:
        """Сверить точку входа в память с тем, что лежит на диске.

        Две беды, и они зеркальные: **сирота** — файл есть, а ссылки на него
        нет ниоткуда (найдёт его только тот, кто уже знает адрес); **битая
        ссылка** — ссылка есть, а файла нет. Валидатор ловит обе, но при записи,
        а запись могла пройти мимо хука — например, файл положил не коуч,
        а `git pull` с другой машины.

        Выжимки из проверки исключены: их список собирает тот же ночной прогон
        между метками в оглавлении, и ругаться на них здесь значило бы ругаться
        на самого себя посреди работы.
        """
        память = self.brain_dir / "память"
        индекс = память / "00-index.md"
        try:
            текст = индекс.read_text(encoding="utf-8")
        except OSError as err:
            log.error("оглавление памяти не читается: %s", err)
            return

        связанные = set(re.findall(r"\[\[([^\]|#]+)", текст))
        файлы = {
            путь.stem: путь
            for путь in память.rglob("*.md")
            if путь.name != "00-index.md" and "выжимки" not in путь.parts
        }
        сироты = sorted(имя for имя in файлы if имя not in связанные)
        битые = sorted(имя for имя in связанные if имя not in файлы)
        if not сироты and not битые:
            log.info("оглавление памяти сходится: %d заметок", len(файлы))
            return
        беда = []
        if сироты:
            беда.append("сироты (файл есть, ссылки нет): " + ", ".join(сироты))
        if битые:
            беда.append("битые ссылки (ссылка есть, файла нет): " + ", ".join(битые))
        строка = "; ".join(беда)
        log.warning("оглавление памяти разошлось: %s", строка)
        await raise_task(self.todoist_token, "оглавление", строка)

    async def _check_load(self, mode, channel: str, summary: str = "", prompt: str = "") -> None:
        """Сверить то, что реально уехало в контекст, с паспортом памяти.

        Сверяем только ПЕРВЫЙ ход сессии: в середине разговора выжимки уже не
        грузятся, а контекст растёт от самих реплик — померили бы болтовню,
        а не рюкзак. Признак первого хода даёт сам движок, а не догадка по
        закладке: к моменту сверки закладка уже перезаписана свежей сессией.

        Двумя сторожами сразу, и это не дубль. Токены отвечают на вопрос
        «во что обошёлся этот режим» и берутся у самой модели. Байты по строкам
        отвечают на другой — «не врёт ли паспорт про отдельную строку»; именно
        так поймали «объявлено 69 400, на деле 37 962». Токенов по строкам
        модель не разбивает, и заменить одно другим нельзя.
        """
        if not self.engine.последний_первый:
            return
        # Построчно сверяем только то, размер чего от режима НЕ зависит.
        # Конституция, файлы стратегии и всё окно целиком меняются по замыслу,
        # и требовать от них одного числа значило бы завести сторожа, который
        # ругается на исправную работу. Их охраняет потолок режима, в токенах.
        куски = len(self.archive.window_rows(mode.окно))
        окно = len(self.archive.recent_digests(mode.окно).encode("utf-8"))
        измерено = {
            # Не сумма окна, а размер куска: сумма растёт сама, пока база
            # наполняется, а размер куска — не растёт. Поехал он — значит
            # выжимки стали многословнее, и чинить надо их промпт.
            "выжимка — средний размер одного куска памяти": окно // куски if куски else 0,
            # Описания кнопок кладёт в контекст движок, а не наш код.
            # Держим строку ради видимости: она объявляет полезную нагрузку,
            # а не цену — цена целиком в токенах, и разница там в восемьдесят раз.
            "описания кнопок инструментов": await self.engine.tools_weight(),
            "сводка дел — агрегат Todoist к каждой реплике": len(summary.encode("utf-8")),
            # Свод завалов уезжает в контекст один раз в сутки — с утренним
            # чек-ином. Считаем его только тогда, когда он там правда есть:
            # в остальные разы это ноль, а не «забыли посчитать».
            "ночной обход дел — свод к утреннему чек-ину": (
                len(prompt.encode("utf-8"))
                - len(prompt.split(detectors.ЗАГОЛОВОК)[0].encode("utf-8"))
                if detectors.ЗАГОЛОВОК in prompt else 0
            ),
        }
        # Ноль означает «этой строки в контексте сейчас нет», а не «она усохла».
        # Свод обхода уезжает раз в сутки, с утренним чек-ином; сверять его
        # с паспортом в остальные двадцать три часа — это ругань на исправную
        # работу, а сторож, который кричит каждый день, приучает себя не читать.
        измерено = {имя: сколько for имя, сколько in измерено.items() if сколько}
        beef = check_budget(self.brain_dir, измерено, mode=mode.имя,
                            tokens=self.engine.последний_контекст)
        if beef:
            log.warning("стартовая загрузка разошлась с паспортом: %s", beef)
            # Сигнализация без реакции — декорация. Лампочка горит → задача с датой.
            await raise_task(self.todoist_token, "потолок", f"[{mode.имя}, {channel}] {beef}")

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
        # Части конституции — та же история и та же цена ошибки. Молча
        # упростившийся коуч хуже отсутствующего: он продолжит отвечать,
        # просто перестанет быть собой, и понять это будет неоткуда.
        нет_частей = режимы_модуль.недостающие_части()
        if нет_частей:
            raise SystemExit(
                "В плагине нет частей конституции: " + ", ".join(нет_частей) +
                f" (папка {режимы_модуль.PARTS_DIR}). Коуч без них — не коуч."
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
        # Вторая дверь: комментарии в карточках задач. Три минуты — компромисс
        # между «ответил, пока не убрал телефон» и ничем: пустой цикл стоит
        # 215 байт и ноль токенов, платить за частоту тут нечем.
        queue.run_repeating(
            self.watch_comments,
            interval=int(env("COMMENTS_POLL_SECONDS", "180")),
            first=30, name="комментарии",
        )

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
