"""Проверка здоровья коуча для docker healthcheck.

Бот — поллер Telegram: наружу он ничего не слушает, поэтому «дёрнуть порт»
здесь нечего. Живость доказываем двумя независимыми признаками:

1. **Пульс.** Главный цикл раз в минуту трогает файл HEARTBEAT_FILE
   (job «пульс» в main.py). Файл свежий — значит жив не просто процесс,
   а именно рабочий цикл: тот же asyncio-loop, что тянет обновления
   из Telegram. Зависший процесс пульс не обновит.
2. **Дорога наружу.** Запрос `getMe` к Telegram через тот же прокси-мост,
   которым ходит бот. Ловит обрыв egress (Xray/подписка) и отозванный токен —
   в обоих случаях бот жив, но глух.

Оба признака обязательны: пульс без сети — бот молчит, сеть без пульса —
цикл встал. Выход 0 — здоров, 1 — нет (docker пометит unhealthy, а сторож
`check-containers.sh` на хосте пришлёт алерт в Telegram).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx

HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/coach-heartbeat"))
# Пульс бьётся раз в 60 с; три пропущенных удара — уже не «моргнуло», а встало.
MAX_AGE_SEC = int(os.environ.get("HEARTBEAT_MAX_AGE", "210"))


def fail(reason: str) -> None:
    print(f"нездоров: {reason}")
    sys.exit(1)


def main() -> None:
    if not HEARTBEAT_FILE.exists():
        fail(f"нет файла пульса {HEARTBEAT_FILE} — главный цикл не дошёл до первого удара")

    age = time.time() - HEARTBEAT_FILE.stat().st_mtime
    if age > MAX_AGE_SEC:
        fail(f"пульс отстал на {int(age)} с (порог {MAX_AGE_SEC} с) — цикл встал")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        fail("в окружении нет TELEGRAM_BOT_TOKEN")

    try:
        answer = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
    except Exception as error:  # noqa: BLE001 — проверка здоровья не должна падать трассировкой
        fail(f"Telegram недоступен через прокси: {type(error).__name__}")
        return

    if not answer.get("ok"):
        # Тело ответа не печатаем целиком: в нём может оказаться эхо токена.
        fail(f"Telegram отверг запрос (error_code={answer.get('error_code')})")

    print(f"здоров: пульс {int(age)} с назад, Telegram отвечает")


if __name__ == "__main__":
    main()
