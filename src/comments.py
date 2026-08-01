"""Канал комментариев Todoist: вторая дверь к коучу — внутри карточки задачи.

Разговор живёт в телеграме. Комментарий — рабочая записка возле дела: Василий
смотрит на задачу, от которой тошно, и пишет прямо под ней «Клод, разбей
на подзадачи». Любимая поломка «нет первого шага» лечится там, где на неё
смотрят, а не в другом приложении.

**Модель просыпается через два сита, и оба — код.**

    будильник 3 мин → /sync (215 Б на пустой цикл)
        ├─ пусто ............................ ноль вызовов, ноль токенов
        └─ есть комментарии
             ├─ свой (реестр id / маркер) ... пропуск
             ├─ чужой (posted_uid ≠ хозяин) . только в архив, как контекст
             ├─ хозяйский без «Клод» ........ только в архив
             └─ хозяйский с «Клод, …» ....... ← здесь и только здесь модель

Требование владельца 01.08: не дёргать модель на опрос. День без обращений
стоит ровно столько же, сколько сегодня, — ноль.

**Отвечает лёгкий работник, а не коуч со всей памятью.** Ему нужна методика
(«как у нас дробят задачи»), а не биография Василия. Полная загрузка — 160 608 Б
в каждой сессии, и платить её за «разбей на подзадачи» незачем. Но главное
не цена: дёргать живую телеграм-сессию из двух мест разом — та самая болезнь
двух писателей, от которой в июле разъехался мозг.

**Ломать он не может.** `complete_task` и `delete_task` работнику не выдаются
вовсе — не «запрещены просьбой в тексте», а физически отсутствуют в наборе.

**Ответ пишет код, а не модель.** Работнику не дают и `add_comment`: его
последняя реплика и есть ответ, а отправляет её код — с маркером и с записью
в реестр. Иначе ответ случался бы, «если модель не забыла», а его собственный
комментарий вернулся бы в следующем опросе неопознанным.

**Что сделано — записано вместе с тем, как было.** Снимок дерева задачи до
и после, разница ложится в журнал (`comment_state.py`), и оттуда работает
откат. Разница снимками, а не разбором ответов: так в журнал попадает всё
изменённое, включая то, чего мы не предусмотрели.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    query,
    tool,
)
from todoist_mcp import plan as plan_module
from todoist_mcp import tools as manifest
from todoist_mcp.client import TodoistClient, TodoistError

from .comment_state import МАРКЕР, Действие, Журнал
from .prompts import load as load_prompt
from .retry import GaveUp, retry_network

log = logging.getLogger(__name__)

MOSCOW = ZoneInfo("Europe/Moscow")
SYNC_URL = "/sync"

# Подпись бота (МАРКЕР) объявлена в comment_state — она нужна и тем, кто пишет
# комментарии мимо канала. Это вторая защита от петли, независимая от реестра
# id: каждая из двух обязана работать в одиночку, иначе это одна защита
# с запасной деталью.

# Обращение по имени. Регистр не важен, знак после имени любой — на телефоне
# запятую ставят не всегда. Имя должно стоять В НАЧАЛЕ: «спрошу у Клода потом»
# не обращение, а рассказ.
ОБРАЩЕНИЕ = re.compile(r"^\s*@?(?:клод|клауд|claude)\b[\s,:;.!?—–-]*", re.IGNORECASE)

# Кнопки работника. Закрытия и удаления здесь нет и не будет: цена ошибки
# в комментарии выше, чем в разговоре, — человек не видит происходящего.
КНОПКИ = ("get_task", "find_tasks", "add_task", "add_tasks_bulk", "update_task", "move_task")

# Поля задачи, за которыми следит журнал: ровно те, что умеем вернуть обратно.
ПОЛЯ = ("content", "description", "priority", "labels", "due_string",
        "project_id", "section_id", "parent_id")

# Сколько обращений в час разбираем. Защита сразу от двух бед: от «написал
# двадцать комментариев подряд» и от петли, которую не поймали обе защиты.
ПОТОЛОК_В_ЧАС = int(os.environ.get("COMMENTS_LIMIT_PER_HOUR", "12"))

МЕТОДИЧКА = Path(os.environ.get("METHODOLOGY_DIR", "/plugin/methodology")) / "работа-в-todoist.md"


def обращение(текст: str) -> str | None:
    """Текст просьбы, если комментарий зовёт по имени. Иначе None."""
    совпадение = ОБРАЩЕНИЕ.match(текст or "")
    if not совпадение:
        return None
    остаток = (текст[совпадение.end():] or "").strip()
    return остаток or None


def _поля(задача: dict[str, Any]) -> dict[str, Any]:
    """Слепок задачи по тем полям, которые умеем вернуть обратно.

    Срок берём строкой Todoist («каждый пн», «завтра»), а не датой: строка
    сохраняет повторяемость, а дата её теряет — откат превратил бы
    повторяющуюся задачу в разовую.
    """
    due = задача.get("due") or {}
    return {
        "content": задача.get("content") or "",
        "description": задача.get("description") or "",
        "priority": задача.get("priority") or 1,
        "labels": sorted(задача.get("labels") or []),
        "due_string": due.get("string") or due.get("date") or "",
        "project_id": задача.get("project_id") or "",
        "section_id": задача.get("section_id") or "",
        "parent_id": задача.get("parent_id") or "",
    }


async def снимок(client: TodoistClient, task_id: str) -> dict[str, dict[str, Any]]:
    """Задача и её подзадачи одним слепком: {id: поля}.

    Дерево, а не одна карточка: «разбей на подзадачи» меняет именно детей.
    Глубже одного уровня не спускаемся намеренно — работник дробит карточку,
    а не перестраивает проект; и снимок обязан быть дешёвым, он делается
    дважды на каждое обращение.
    """
    задача = await client.get(f"/tasks/{task_id}")
    дети = await client.get_paginated("/tasks", params={"parent_id": str(task_id)})
    слепок = {str(task_id): _поля(задача)}
    for ребёнок in дети or []:
        слепок[str(ребёнок["id"])] = _поля(ребёнок)
    return слепок


def разница(до: dict[str, dict], после: dict[str, dict]) -> list[tuple[str, str, str, Any, Any]]:
    """Что изменилось: список (вид, task_id, поле, было, стало).

    Удаление в список не попадает: удалять работник не умеет. Если задача
    всё-таки исчезла (человек убрал её сам, пока работник думал) — это
    не наше действие, и откатывать его нельзя.
    """
    итог: list[tuple[str, str, str, Any, Any]] = []
    for task_id, поля in после.items():
        if task_id not in до:
            итог.append(("создана", task_id, "", None, поля))
            continue
        for имя, стало in поля.items():
            было = до[task_id].get(имя)
            if было != стало:
                итог.append(("изменено", task_id, имя, было, стало))
    return итог


def рабочий_набор(token: str):
    """Набор кнопок работника: только разрешённое, всё остальное выключено явно.

    Тумблеры по умолчанию включают кнопку, которой нет в списке (решение
    от 01.08: «кнопка, которой нет в файле тумблеров, обязана работать»).
    Поэтому здесь перечисляются ВСЕ кнопки манифеста с явным да/нет —
    новая кнопка в манифесте не просочится к работнику молча.
    """
    switches = {t.name: (t.name in КНОПКИ) for t in manifest.TOOLS}
    from .todoist import build_todoist_server  # общий сборщик, один дом кнопок

    return build_todoist_server(token, switches)


class Канал:
    """Опрос комментариев, работник и журнал действий в одном доме."""

    def __init__(
        self,
        token: str,
        db_path: Path | str | None = None,
        archive=None,
        model: str = "claude-fable-5",
        сказать=None,
    ) -> None:
        self.token = token
        self.журнал = Журнал(db_path)
        self.archive = archive
        self.model = model
        # Чем жаловаться владельцу в телеграм, когда канал упёрся в потолок.
        self.сказать = сказать
        self._хозяин: str | None = None
        self._предупредил_о_потолке = False
        # Набор кнопок работника собирается один раз: сборка спрашивает тариф
        # у Todoist, и делать это на каждое обращение — лишний запрос впустую.
        self._набор = None
        # Будильник тикает каждые три минуты, а разбор обращения занимает
        # десятки секунд. Без замка второй цикл влез бы в середину первого
        # и разобрал бы то же обращение второй раз.
        self._занят = False

    # --- опрос ---

    async def _uid_хозяина(self, client: TodoistClient) -> str:
        """Кто владелец токена. Спрашиваем у Todoist, а не пишем в конфиг.

        Тот же принцип, что с тарифом: записанное руками соврёт в день, когда
        поменяется, а спрошенное — нет.
        """
        if self._хозяин is None:
            данные = await client.get("/user")
            self._хозяин = str(данные.get("id") or "")
        return self._хозяин

    async def _спросить(self, client: TodoistClient) -> tuple[list[dict], bool]:
        """Новые комментарии по закладке. Возвращает (список, первый_ли_раз).

        Первый запуск токена не имеет и получает полную выгрузку — её
        разбирать нельзя: четырнадцать старых комментариев превратились бы
        в четырнадцать обращений. Берём закладку и молчим.
        """
        закладка = self.журнал.токен()
        ответ = await client.post(
            SYNC_URL,
            data={"sync_token": закладка or "*", "resource_types": '["notes"]'},
        )
        свежий = ответ.get("sync_token")
        if свежий:
            self.журнал.запомнить_токен(свежий)
        первый = закладка is None
        заметки = [n for n in (ответ.get("notes") or []) if not n.get("is_deleted")]
        return ([], True) if первый else (заметки, False)

    async def разобрать(self, заметки: list[dict], хозяин: str) -> list[dict]:
        """Просеять комментарии: что в архив, что в работу, что мимо."""
        обращения: list[dict] = []
        своих = 0
        for заметка in sorted(заметки, key=lambda n: n.get("posted_at") or ""):
            текст = (заметка.get("content") or "").strip()
            comment_id = str(заметка.get("id") or "")
            if not текст or not comment_id:
                continue
            # Сито 1: своё. Реестр и маркер — две независимые защиты.
            if self.журнал.свой(comment_id) or МАРКЕР in текст:
                своих += 1
                continue
            свой_ли_автор = str(заметка.get("posted_uid") or "") == хозяин
            # Сито 2: чужое. Читаем как контекст (коуч вечером в курсе,
            # о чём речь), но обращение чужого не срабатывает: память
            # у коуча про Василия, работать на второго человека он не должен.
            await self._в_архив(
                "vasiliy" if свой_ли_автор else "coach",
                f"[комментарий{'' if свой_ли_автор else ', не Василий'} "
                f"к задаче {заметка.get('item_id')}] {текст}",
            )
            if not свой_ли_автор:
                continue
            # Сито 3: позвали ли по имени.
            просьба = обращение(текст)
            if просьба:
                обращения.append({
                    "id": comment_id,
                    "task_id": str(заметка.get("item_id") or ""),
                    "текст": просьба,
                })
        if своих:
            log.info("канал комментариев: пропущено своих — %d", своих)
        return обращения

    async def _в_архив(self, роль: str, текст: str) -> None:
        """Комментарии ложатся в тот же архив, что и телеграм.

        Тогда вечером коуч знает, о чём была речь в задачах, а ночная выжимка
        видит день целиком. Следствие названо осознанно: реплика в канале
        `comment` считается признаком жизни и отменяет дожим — человек занят
        делами, дёргать его «приём-приём» незачем.
        """
        if self.archive is None:
            return
        try:
            await self.archive.add_message(роль, "comment", текст, None)
        except Exception:
            log.exception("не смог записать комментарий в архив")

    # --- работа ---

    async def шаг(self) -> dict[str, int]:
        """Один цикл: опросить, просеять, разобрать обращения.

        Ничего не найдено — ни одного вызова модели. Это и есть главное
        свойство канала, а не побочное.
        """
        итог = {"новых": 0, "обращений": 0, "сделано": 0}
        if self._занят:
            log.info("канал комментариев: прошлый цикл ещё работает, пропускаю")
            return итог
        self._занят = True
        try:
            return await self._шаг()
        finally:
            self._занят = False

    async def _шаг(self) -> dict[str, int]:
        итог = {"новых": 0, "обращений": 0, "сделано": 0}
        try:
            async with TodoistClient(self.token) as client:
                хозяин = await self._uid_хозяина(client)
                заметки, первый = await self._спросить(client)
                if первый:
                    log.info("канал комментариев: закладка поставлена, история не разбирается")
                    return итог
                итог["новых"] = len(заметки)
                обращения = await self.разобрать(заметки, хозяин)
        except (TodoistError, OSError) as err:
            log.warning("канал комментариев: опрос не удался: %s", err)
            return итог

        итог["обращений"] = len(обращения)
        for просьба in обращения:
            if not await self._в_пределах_потолка():
                break
            if await self._выполнить(просьба):
                итог["сделано"] += 1
        return итог

    async def _в_пределах_потолка(self) -> bool:
        разобрано = await asyncio.to_thread(
            self.журнал.обращений_за_час, datetime.now(MOSCOW)
        )
        if разобрано < ПОТОЛОК_В_ЧАС:
            self._предупредил_о_потолке = False
            return True
        log.error("канал комментариев: потолок %d обращений в час", ПОТОЛОК_В_ЧАС)
        if not self._предупредил_о_потолке:
            self._предупредил_о_потолке = True
            await self._пожаловаться(
                f"В комментариях за час набралось {разобрано} обращений — "
                f"это потолок ({ПОТОЛОК_В_ЧАС}). Пока притормозил: либо их правда "
                f"много, либо я зациклился. Посмотри логи канала."
            )
        return False

    async def _пожаловаться(self, текст: str) -> None:
        if self.сказать is None:
            return
        try:
            await self.сказать(текст)
        except Exception:
            log.exception("не смог пожаловаться владельцу")

    async def _выполнить(self, просьба: dict) -> bool:
        """Разобрать одно обращение: снимок до → работник → снимок после → ответ."""
        task_id = просьба["task_id"]
        request_id = просьба["id"]
        await asyncio.to_thread(
            self.журнал.отметить_обращение, request_id, task_id, просьба["текст"]
        )
        try:
            async with TodoistClient(self.token) as client:
                карточка = await retry_network(
                    lambda: self._карточка(client, task_id), what="чтение карточки задачи"
                )
                до = await retry_network(
                    lambda: снимок(client, task_id), what="снимок задачи до работы"
                )
        except (TodoistError, GaveUp) as err:
            log.warning("канал комментариев: задача %s не читается: %s", task_id, err)
            await self.ответить(task_id, f"Не смог открыть задачу: {err}", request_id)
            return False

        ответ = await self._работник(карточка, просьба["текст"], task_id)

        # Снимок «после» — не формальность: без него журнал пуст, а пустой журнал
        # означает «ничего не менялось». Это ложь того самого класса, что уже
        # ловили дважды: сбой, выглядящий как успех (прогон 01.08.2026 — обрыв
        # соединения дал «изменений 0» при шести созданных подзадачах, и откат
        # потом вернул только комментарий). Поэтому здесь повтор, а провал
        # повтора — громкий: человек обязан узнать, что откатить это уже нечем.
        после, слепой = None, ""
        try:
            async with TodoistClient(self.token) as client:
                после = await retry_network(
                    lambda: снимок(client, task_id), what="снимок задачи после работы"
                )
        except (TodoistError, GaveUp) as err:
            log.error("канал комментариев: не снял состояние после работы: %s", err)
            слепой = ("\n\n⚠️ Записать, что именно я поменял, не смог — Todoist не ответил. "
                      "Откатить это одной фразой не выйдет, придётся руками.")
            await self._пожаловаться(
                f"В задаче {task_id} я поработал по комментарию, но не смог снять "
                f"состояние после: {err}. Журнал действий пуст — откат невозможен."
            )
            после = до

        изменения = разница(до, после)
        for вид, изменённая, поле, было, стало in изменения:
            await asyncio.to_thread(
                self.журнал.записать, request_id, изменённая, вид, поле, было, стало
            )
        log.info(
            "канал комментариев: обращение %s к задаче %s, изменений %d",
            request_id, task_id, len(изменения),
        )
        await self.ответить(task_id, (ответ or "Сделал.") + слепой, request_id)
        return True

    @staticmethod
    async def _карточка(client: TodoistClient, task_id: str) -> str:
        from todoist_mcp import core

        return await core.get_task(client, task_id, include_subtasks=True, include_comments=True)

    async def _работник(self, карточка: str, просьба: str, task_id: str) -> str:
        """Короткий прогон модели: методика, карточка, просьба — и всё.

        Ни окна выжимок, ни конституции, ни нити телеграм-разговора.
        """
        промпт = load_prompt("комментарий")
        методичка = ""
        try:
            методичка = МЕТОДИЧКА.read_text(encoding="utf-8")
        except OSError:
            # Методичка живёт в плагине. Нет её — работаем без неё, но вслух:
            # молча упростившийся работник хуже, чем отсутствующий.
            log.error("методичка работы в Todoist не читается: %s", МЕТОДИЧКА)

        текст = промпт.format(
            task_id=task_id, карточка=карточка, просьба=просьба, методичка=методичка,
        )
        if self._набор is None:
            self._набор = рабочий_набор(self.token)
        options = ClaudeAgentOptions(
            model=self.model,
            effort="medium",
            system_prompt=промпт.system,
            mcp_servers={"todoist": self._набор},
            allowed_tools=["mcp__todoist"],
            permission_mode="bypassPermissions",
        )
        куски: list[str] = []
        try:
            async for сообщение in query(prompt=текст, options=options):
                if isinstance(сообщение, AssistantMessage):
                    for блок in сообщение.content:
                        if isinstance(блок, TextBlock):
                            куски.append(блок.text)
                elif isinstance(сообщение, ResultMessage) and сообщение.is_error:
                    log.error("работник комментариев не справился: %s", сообщение.result)
        except Exception as err:
            log.exception("работник комментариев сорвался")
            return f"Не справился: {type(err).__name__}: {err}"
        return "\n".join(к.strip() for к in куски if к.strip()).strip()

    # --- ответ ---

    async def ответить(self, task_id: str, текст: str, request_id: str = "") -> str | None:
        """Написать ответ комментарием. Возвращает id или None.

        Порядок важен: сначала отправляем, потом запоминаем id. Наоборот
        нельзя — id до отправки неизвестен; на этот зазор и стоит вторая
        защита, маркер в тексте.
        """
        тело = f"{МАРКЕР} {текст.strip()}"
        try:
            async with TodoistClient(self.token) as client:
                комментарий = await retry_network(
                    lambda: client.post(
                        "/comments", json={"task_id": str(task_id), "content": тело}
                    ),
                    what="отправка ответа комментарием",
                )
        except (TodoistError, GaveUp, OSError) as err:
            log.error("канал комментариев: ответ не отправлен: %s", err)
            return None
        comment_id = str(комментарий.get("id") or "")
        await asyncio.to_thread(self.журнал.запомнить_свой, comment_id)
        if request_id:
            await asyncio.to_thread(
                self.журнал.записать, request_id, task_id, "комментарий",
                "", None, comment_id,
            )
        await self._в_архив("coach", f"[ответ в задаче {task_id}] {текст.strip()}")
        return comment_id

    # --- откат ---

    async def откатить(self, request_id: str | None = None) -> str:
        """Вернуть, как было, всё сделанное по последнему обращению."""
        request_id = request_id or await asyncio.to_thread(self.журнал.последнее_обращение)
        if not request_id:
            return "Откатывать нечего: по комментариям я ничего не менял."
        действия = await asyncio.to_thread(self.журнал.действия, request_id)
        if not действия:
            return "Откатывать нечего: последнее обращение ничего не изменило."

        сделано: list[str] = []
        отменённые: list[int] = []
        try:
            async with TodoistClient(self.token) as client:
                for действие in действия:  # свежие первыми: разбираем с конца
                    строка = await self._вернуть(client, действие)
                    if строка:
                        сделано.append(строка)
                        отменённые.append(действие.id)
        except (TodoistError, OSError) as err:
            log.exception("откат сорвался")
            await asyncio.to_thread(self.журнал.пометить_откат, отменённые)
            return (f"Откатил частично ({len(отменённые)} из {len(действия)}), "
                    f"дальше сорвалось: {err}")

        await asyncio.to_thread(self.журнал.пометить_откат, отменённые)
        return "Вернул как было:\n" + "\n".join(f"– {с}" for с in сделано)

    @staticmethod
    async def _вернуть(client: TodoistClient, действие: Действие) -> str:
        """Одно действие обратно. Возвращает строку для отчёта или пустую."""
        if действие.kind == "создана":
            имя = (действие.after or {}).get("content", действие.task_id)
            await client.delete(f"/tasks/{действие.task_id}")
            return f"удалил созданную задачу «{имя}»"
        if действие.kind == "комментарий":
            if действие.after:
                await client.delete(f"/comments/{действие.after}")
            return "убрал свой комментарий"
        if действие.kind == "изменено":
            поле, было = действие.field, действие.before
            if поле in ("project_id", "section_id", "parent_id"):
                # Место задачи меняется только переносом, и пустое значение
                # переносом не выражается: «убрать родителя» — это «перенести
                # в проект». Поэтому у пустого значения спрашиваем, куда именно.
                if было:
                    тело = {поле: было}
                else:
                    задача = await client.get(f"/tasks/{действие.task_id}")
                    тело = {"project_id": задача.get("project_id")}
                await client.post(f"/tasks/{действие.task_id}/move", json=тело)
            elif поле == "due_string":
                # Пустой срок снимается словами: Todoist понимает «no date».
                await client.post(
                    f"/tasks/{действие.task_id}",
                    json={"due_string": было or "no date", "due_lang": "ru"},
                )
            else:
                await client.post(f"/tasks/{действие.task_id}", json={поле: было})
            return f"вернул {поле}: «{действие.after}» → «{было}»"
        return ""


def build_undo_server(канал: Канал):
    """Кнопка отката для коуча: «откати последнее действие» работает словами.

    Дверь проекта — сказать, а не помнить команду. Цена кнопки известна
    (≈750 Б в каждой сессии) и объявлена в паспорте; кончится запас —
    выключается тумблером, а не правкой кода.
    """

    @tool(
        "undo_last",
        "Откатить последнее, что ты сделал по комментарию в Todoist: удалить "
        "созданные подзадачи и вернуть прежние сроки, приоритеты, метки, текст. "
        "Звать, когда Василий говорит «откати», «верни как было», «убери то, "
        "что ты там создал». Единица отката — одно его обращение целиком.",
        {},
    )
    async def undo_last(args: dict[str, Any]) -> dict[str, Any]:
        текст = await канал.откатить()
        return {"content": [{"type": "text", "text": текст}]}

    return create_sdk_mcp_server(name="undo", version="1.0.0", tools=[undo_last])
