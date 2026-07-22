#!/usr/bin/env python3
"""Отправка разговоров из VS Code в архив бота-коуча на Bronto.

Из транскриптов Claude Code проекта gtd выжимаются только реплики Василия и
текстовые ответы коуча (мысли, вызовы инструментов, терминал — шум, его не
берём) и уходят по ssh в SQLite бота. Там их подхватывает ночная выжимка.

Запускается launchd каждые 2 минуты (com.vefmv.coach-dialog-sync).
Дважды защищено от дублей: локально — байтовыми смещениями по файлам,
на сервере — уникальным индексом по uuid. Сбой на любом шаге не страшен:
следующий прогон дошлёт, лишнего не запишется.
"""

from __future__ import annotations  # launchd зовёт системный python 3.9 — он не знает «dict | None»

import json
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path.home() / ".claude/projects/-Users-vasiliy-Yandex-Disk-localized-Claude-Code-gtd"
STATE_FILE = Path.home() / ".claude/state/coach-dialog-sync.json"
LOG_FILE = Path.home() / ".claude/logs/coach-dialog-sync.log"
LOCK_DIR = Path("/tmp/coach-dialog-sync.lock")
SSH_CMD = [
    "/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
    "bronto", "docker", "exec", "-i", "coach-bot", "python", "-m", "src.import_laptop",
]
# Служебные тексты, которые Claude Code кладёт в транскрипт под видом реплик Василия:
# вызовы команд, напоминания харнесса и уведомления фоновых агентов. Пропускать их
# нельзя вдвойне — они и мусорят выжимку, и выглядят как «человек отозвался».
SERVICE_PREFIXES = (
    "<command-name>", "<local-command", "<system-reminder>", "Caveat:",
    "<task-notification>", "[SYSTEM NOTIFICATION", "[Request interrupted",
)


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    # Лог не должен расти вечно
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > 2000:
            LOG_FILE.write_text("".join(lines[-500:]), encoding="utf-8")
    except OSError:
        pass


def take_lock() -> bool:
    # mkdir атомарен; замок старше 10 минут — от убитого прогона, снимаем
    try:
        LOCK_DIR.mkdir()
        return True
    except FileExistsError:
        if time.time() - LOCK_DIR.stat().st_mtime > 600:
            try:
                LOCK_DIR.rmdir()
                LOCK_DIR.mkdir()
                return True
            except OSError:
                return False
        return False


def extract(record: dict) -> dict | None:
    """Реплика Василия или текст коуча — или None, если это шум."""
    if record.get("isSidechain") or record.get("isMeta"):
        return None
    rtype = record.get("type")
    uuid = record.get("uuid")
    ts = record.get("timestamp")
    if not uuid or not ts:
        return None
    content = (record.get("message") or {}).get("content")

    if rtype == "user":
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # сообщение с картинкой: берём текстовые блоки; tool_result-записи отпадут сами
            text = "\n".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            return None
        text = text.strip()
        if not text or text.startswith(SERVICE_PREFIXES):
            return None
        return {"uuid": uuid, "ts": ts, "role": "vasiliy", "text": text}

    if rtype == "assistant" and isinstance(content, list):
        text = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if not text:
            return None
        return {"uuid": uuid, "ts": ts, "role": "coach", "text": text}

    return None


def main() -> None:
    if not PROJECT_DIR.is_dir():
        return
    if not take_lock():
        return
    try:
        state = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {}
        offsets = state.get("files", {})

        batch: list[str] = []
        new_offsets = dict(offsets)
        for path in sorted(PROJECT_DIR.glob("*.jsonl")):
            start = int(offsets.get(path.name, 0))
            size = path.stat().st_size
            if size <= start:
                continue
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read()
            # последняя строка может писаться прямо сейчас — берём только завершённые
            end = chunk.rfind(b"\n")
            if end < 0:
                continue
            for raw in chunk[: end + 1].splitlines():
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                item = extract(record)
                if item:
                    batch.append(json.dumps(item, ensure_ascii=False))
            new_offsets[path.name] = start + end + 1

        if not batch:
            return

        if "--dry-run" in sys.argv:
            for line in batch:
                print(line[:200])
            print(f"— всего: {len(batch)} (dry-run, ничего не отправлено)")
            return

        proc = subprocess.run(
            SSH_CMD, input="\n".join(batch) + "\n",
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode == 0:
            # смещения двигаем только после подтверждённой доставки
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps({"files": new_offsets}, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            log(f"отправлено {len(batch)} → {proc.stdout.strip()}")
        else:
            log(f"отправка не прошла (код {proc.returncode}), дошлю в следующий раз: {proc.stderr.strip()[:200]}")
    finally:
        try:
            LOCK_DIR.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    main()
