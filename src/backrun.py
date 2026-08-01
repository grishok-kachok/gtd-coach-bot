"""Фоновый прогон: одна дверь для всей работы, которую делает не разговор.

Ночная выжимка, ночная проверка памяти, подпись истории и работник комментариев
устроены одинаково: короткий промпт, короткий системный текст, один ответ.
До этого этапа каждый собирал себе настройки движка сам, и «сам» означало
«кто как вспомнил»: три из четырёх гасили встроенные инструменты, четвёртый —
нет; ни один не гасил правила памяти, потому что об этом рычаге просто
не думали.

Теперь настройки берутся из **фонового режима** (`режимы.md` плагина), и это
не косметика: там гасятся встроенные инструменты (3 781 токен в каждом запросе)
и правила памяти с навыками плагина (4 118). Прогону, который сворачивает день
в текст, ни то ни другое не нужно — он не лазит по файлам и ничего не пишет
в знания.

Чего здесь НЕ происходит: конституция сюда не попадала никогда. Замысел этапа
исходил из того, что «каждый фоновый прогон тащит полную конституцию», —
чтение кода это не подтвердило, и замер тоже (см. RESULT.md этапа 17). Пишем
как есть: рычага «снять конституцию» у фоновых прогонов не было, потому что
конституции у них и не было.

Цена каждого прогона ложится в тот же журнал, что и цена разговора: без этого
«фоновый подешевел» осталось бы утверждением, а не числом.
"""

from __future__ import annotations

import logging
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .modes import Режим

log = logging.getLogger(__name__)


def настройки(mode: Режим, **свои: Any) -> ClaudeAgentOptions:
    """Настройки движка для фонового прогона: рычаги режима плюс своё.

    Рычаги режима идут ПОСЛЕДНИМИ и переспорить их вызывающим нельзя —
    иначе через полгода один из прогонов снова окажется дороже прочих,
    и никто не поймёт почему.
    """
    свои.pop("tools", None)
    свои.pop("setting_sources", None)
    return ClaudeAgentOptions(
        **свои,
        # Пустой список — это НЕ «умолчание»: он гасит Read, Write, Bash и прочее.
        tools=None if mode.встроенные_инструменты else [],
        # То же и с правилами: None означало бы «как настроено у пользователя»,
        # то есть непредсказуемо. Фоновому прогону правила записи не нужны.
        setting_sources=["project"] if mode.правила_памяти else [],
    )


async def прогон(*, mode: Режим, prompt: str, system: str, model: str,
                 effort: str = "high", cost=None, channel: str = "фоновый",
                 что: str = "фоновый прогон", **свои: Any) -> str:
    """Спросить модель один раз и вернуть текст. Цену записать.

    Ошибку не глотаем и не превращаем в пустую строку молча: она уходит
    в лог с именем прогона, чтобы ночная поломка не выглядела как «день
    был пустой».
    """
    options = настройки(mode, system_prompt=system, model=model, effort=effort, **свои)
    куски: list[str] = []
    usage: object = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            for блок in message.content:
                if isinstance(блок, TextBlock):
                    куски.append(блок.text)
        elif isinstance(message, ResultMessage):
            usage = message.usage
            if message.is_error:
                log.error("%s не удался: %s", что, message.result)
    await записать_цену(cost, channel, mode, model, usage, что)
    return "\n".join(к.strip() for к in куски if к.strip()).strip()


async def записать_цену(cost, channel: str, mode: Режим, model: str,
                        usage: object, что: str) -> None:
    """Отметить цену фонового хода. Поломка счётчика ночную работу не рвёт."""
    from .context_cost import контекст, разобрать

    числа = разобрать(usage)
    if not числа:
        return
    log.info("контекст: %s, режим %s, %d токенов, выдано %d",
             что, mode.имя, контекст(числа), числа["output"])
    if cost is None:
        return
    try:
        await cost.записать(channel, mode.имя, model, usage)
    except Exception:
        log.exception("не смог записать цену фонового прогона")
