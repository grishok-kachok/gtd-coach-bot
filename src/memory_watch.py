"""Ночная проверка памяти: не пора ли её обновить и где она за день подвела.

Зачем это вообще. Разбор сессии 31.07 нашёл протечку: коуч каждый день
пользовался наблюдением «ставка на кон заводит», а в модуле знаний его не было
вовсе — оно жило только в выжимках, то есть в окне, которое уезжает вперёд.
Причина не в лени модели, а в разнице механизмов: выжимку пишет код по
будильнику и она случается всегда, а запись в `знания/` держалась на просьбе
в тексте конституции — «увидел паттерн, допиши» — и случалась через раз.

Лекарство ровно одно: повесить проверку памяти на тот же будильник. Ночью,
сразу после выжимки, день перечитывается второй раз — но другими глазами:
не «что произошло», а «что из этого должно осесть в памяти и где память
сегодня не сработала».

Две вещи, которые здесь НЕ делаются намеренно:

1. **Ничего не пишется в `знания/` молча.** Граница из PROJECT.md: факт сказал
   человек — пишется сразу; вывод придумал бот — только после подтверждения.
   Ночью бот человека не спросит, поэтому ночь только предлагает. Записывает
   коуч в разговоре, когда Василий подтвердил.
2. **Бот не оценивает сам себя.** Исходная идея была «пусть ночью оценивает,
   насколько адекватны критерии выжимки». Так не работает: оценивать нечем —
   он не знает, что выпало, потому что выпавшего у него уже нет. Поэтому мерим
   не самооценку, а **промахи** — наблюдаемые события: полез в архив за тем,
   что должно было быть в выжимке; переспросил то, что Василий уже говорил.
   Через месяц это список улик, по которому критерии меняет человек
   в мастерской, а не бот у себя в голове.
"""

from __future__ import annotations

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

PROMPT = """Ниже — что коуч знает про Василия сегодня, и стенограмма разговора за {day}.

Твоя работа — не пересказать день (это уже сделано выжимкой), а ответить
на два вопроса.

**1. Что из этого дня должно осесть в памяти, но её ещё нет?**
Сравни разговор с тем, что уже записано в знаниях. Разложи найденное на два вида:

- **Факты со слов Василия** — он сам это сказал: даты, имена, суммы, решения,
  «мне не нравится, когда…», «я так не работаю». Такое пишется в память сразу.
- **Выводы** — то, что ты вывел сам, наблюдая за ним: паттерны, повторы,
  «похоже, его тормозит…». Такое требует подтверждения у Василия.

Не предлагай то, что уже записано. Не предлагай разовые эпизоды — память
про человека, а не про день. Нечего предложить — так и напиши, это нормальный
исход, а не провал.

**2. Где память сегодня подвела?**
Промах — наблюдаемое событие, а не ощущение. Считаются два вида:
- коуч полез в архив или переспросил за тем, что должно было быть в памяти;
- Василий поправил коуча или повторил то, что уже говорил раньше.
Для каждого промаха: что именно искали, где это должно было лежать.

Отвечай строго в таком виде, без вступлений и без markdown-заголовков сверху:

ФАКТЫ:
- ...

ВЫВОДЫ:
- ...

ПРОМАХИ:
- ...

Пустой раздел пиши как «- нет».

=== ЧТО УЖЕ ЗАПИСАНО В ПАМЯТИ ===

{knowledge}

=== СТЕНОГРАММА ДНЯ {day} ===

{transcript}
"""

PROPOSALS = "предложения-памяти.md"
MISSES = "промахи-памяти.md"

HEAD = """---
title: {title}
type: source
schema_version: "1.0"
status: stable
created: {created}
source_type: personal-experience
reliability: C
author: агент-коуч (ночная проверка памяти)
ref: {ref}
root_id: [{root}]
tags: [проверка-памяти]
---

# {heading}

> {note}

"""


class MemoryWatch:
    """Второй ночной вопрос к прожитому дню — про саму память."""

    def __init__(self, brain_dir: Path, model: str = "claude-fable-5") -> None:
        self.brain_dir = brain_dir
        self.model = model
        self.journal = brain_dir / "память" / "журнал"

    async def _ask(self, prompt: str) -> str:
        options = ClaudeAgentOptions(
            model=self.model,
            effort="high",  # раз в сутки — экономить нечего
            tools=[],       # думать, а не лазить по файлам
            system_prompt=(
                "Ты следишь за памятью коуча: что в неё пора записать и где она "
                "сегодня не сработала. Пишешь по-русски, коротко и по фактам."
            ),
        )
        parts: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(message, ResultMessage) and message.is_error:
                log.error("проверка памяти не удалась: %s", message.result)
        return "\n".join(p.strip() for p in parts if p.strip()).strip()

    def _knowledge(self) -> str:
        """Что коуч знает про Василия сегодня — чтобы не предлагать записанное."""
        parts = []
        for folder in ("знания", "состояние"):
            for path in sorted((self.brain_dir / "память" / folder).glob("*.md")):
                parts.append(f"--- {folder}/{path.name} ---\n{path.read_text(encoding='utf-8')}")
        return "\n\n".join(parts)

    @staticmethod
    def _section(answer: str, name: str) -> list[str]:
        """Достать раздел ответа. Разбор нарочно тупой: модель отвечает по форме."""
        lines = []
        inside = False
        for line in answer.splitlines():
            head = line.strip().rstrip(":").upper()
            if head in ("ФАКТЫ", "ВЫВОДЫ", "ПРОМАХИ"):
                inside = head == name
                continue
            if inside and line.strip().startswith("-"):
                body = line.strip()[1:].strip()
                if body and body.lower() != "нет":
                    lines.append(body)
        return lines

    def _append(self, name: str, title: str, heading: str, note: str, ref: str,
                day: date, block: str) -> Path:
        path = self.journal / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                HEAD.format(title=title, created=day.isoformat(), ref=ref,
                            root=f"разговор-{day.isoformat()}", heading=heading, note=note),
                encoding="utf-8",
            )
        with path.open("a", encoding="utf-8") as f:
            f.write(block)
        return path

    async def run(self, day: date, transcript: str) -> dict[str, int]:
        """Перечитать день глазами памяти. Возвращает, сколько чего нашлось."""
        if len(transcript) < 200:
            return {"факты": 0, "выводы": 0, "промахи": 0}

        answer = await self._ask(
            PROMPT.format(day=day.isoformat(), knowledge=self._knowledge(), transcript=transcript)
        )
        facts = self._section(answer, "ФАКТЫ")
        conclusions = self._section(answer, "ВЫВОДЫ")
        misses = self._section(answer, "ПРОМАХИ")

        if facts or conclusions:
            block = f"\n## {day.isoformat()}\n\n"
            if facts:
                block += "**Со слов Василия — можно записывать сразу:**\n"
                block += "".join(f"- {item}\n" for item in facts) + "\n"
            if conclusions:
                block += "**Выводы коуча — сначала спросить Василия:**\n"
                block += "".join(f"- {item}\n" for item in conclusions) + "\n"
            self._append(
                PROPOSALS, "предложения-памяти", "Что стоит записать в память",
                "Собирает ночной прогон (`src/memory_watch.py`). Это ВХОДЯЩИЕ, а не память: "
                "факты со слов Василия коуч переносит в `знания/` сам, выводы — только "
                "после того, как Василий подтвердил. Перенёс — вычеркни строку здесь.",
                "ночная проверка памяти, накопительный список", day, block,
            )

        if misses:
            block = f"\n## {day.isoformat()}\n\n" + "".join(f"- {item}\n" for item in misses)
            self._append(
                MISSES, "промахи-памяти", "Копилка промахов памяти",
                "Собирает ночной прогон (`src/memory_watch.py`). Промах — наблюдаемое событие: "
                "коуч полез в архив за тем, что должно было быть в памяти, или переспросил "
                "сказанное. Это улики для человека в мастерской: по ним меняются критерии "
                "выжимки. Бот свои критерии не меняет.",
                "ночная проверка памяти, накопительный список улик", day, block,
            )

        found = {"факты": len(facts), "выводы": len(conclusions), "промахи": len(misses)}
        log.info("ночная проверка памяти за %s: %s", day, found)
        return found
