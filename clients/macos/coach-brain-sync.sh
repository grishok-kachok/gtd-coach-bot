#!/bin/bash
# Бесшовная синхронизация мозга коуча с GitHub.
#
# Мозг правят с трёх сторон: Василий в VS Code, коуч в Claude Code и бот на
# сервере. Скрипт держит их в согласии — забирает чужое, отдаёт своё.
# Запускается через launchd: при изменении файлов и раз в 2 минуты подстраховкой.

set -uo pipefail

REPO="/Users/vasiliy/Yandex.Disk.localized/Claude Code/gtd"
LOG="/Users/vasiliy/.claude/logs/coach-brain-sync.log"
LOCK="/tmp/coach-brain-sync.lock"

STUCK_MARK="/tmp/coach-brain-sync.stuck"

mkdir -p "$(dirname "$LOG")"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# Сообщить владельцу, что синхронизация встала. Раз в 6 часов, чтобы не долбить.
notify_stuck() {
    if [ -f "$STUCK_MARK" ] && [ -z "$(find "$STUCK_MARK" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
        return
    fi
    token=$(security find-generic-password -s "vefmv/gtd-bot-telegram" -w 2>/dev/null) || return
    [ -n "$token" ] || return
    curl -s --max-time 15 -o /dev/null \
        "https://api.telegram.org/bot$token/sendMessage" \
        --data-urlencode "chat_id=337671692" \
        --data-urlencode "text=⚠️ Синхронизация памяти встала: на ноутбуке и на сервере правили разное, git сам не разберётся. Файлы целы, но правки пока не расходятся между устройствами. Загляни в ~/.claude/logs/coach-brain-sync.log" \
        && touch "$STUCK_MARK" && log "владельцу отправлено предупреждение о расхождении"
}

# Два прогона разом устроили бы гонку за индекс git — пропускаем, следующий догонит.
# mkdir атомарен; flock на macOS нет.
if ! mkdir "$LOCK" 2>/dev/null; then
    # Замок от убитого прогона не должен держать синхронизацию вечно
    if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]; then
        rm -rf "$LOCK" && mkdir "$LOCK" 2>/dev/null || exit 0
    else
        exit 0
    fi
fi
trap 'rm -rf "$LOCK"' EXIT

cd "$REPO" 2>/dev/null || { log "нет папки $REPO"; exit 1; }
[ -d .git ] || { log "$REPO не под git"; exit 1; }

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
git config user.name  >/dev/null || git config user.name "Василий"
git config user.email >/dev/null || git config user.email "pelatihanev@gmail.com"

# 1. Сначала сохранить своё — до любых манипуляций с историей.
if [ -n "$(git status --porcelain)" ]; then
    files=$(git status --porcelain | wc -l | tr -d ' ')
    git add -A
    git commit -q -m "Автосохранение мозга: $(date '+%d.%m %H:%M') (файлов: $files)" || true
    log "закоммичено файлов: $files"
fi

# 2. Забрать чужое (правки бота).
if ! pull_out=$(git pull --rebase -q 2>&1); then
    git rebase --abort 2>/dev/null
    # Ночью бот сворачивает день в один осмысленный коммит — история переписана.
    # Принимаем её, только если содержимое файлов совпало байт в байт: тогда
    # теряется лишь дублирующая цепочка подписей, а не работа.
    git fetch -q 2>/dev/null
    if [ "$(git rev-parse HEAD^{tree} 2>/dev/null)" = "$(git rev-parse '@{u}^{tree}' 2>/dev/null)" ]; then
        git reset --hard '@{u}' -q && log "принял свёрнутую ботом историю (содержимое не изменилось)"
    else
        # Настоящее расхождение: и здесь, и на сервере правили разное. Данные целы,
        # но синхронизация встала — молчать нельзя, иначе мозг тихо разъедется.
        log "РАСХОЖДЕНИЕ: pull не прошёл и содержимое различается — нужна рука. ${pull_out:-}"
        notify_stuck
    fi
fi

# 3. Отправить, если есть что
if [ -n "$(git log '@{u}..HEAD' --oneline 2>/dev/null)" ]; then
    if push_out=$(git push -q 2>&1); then
        log "отправлено на GitHub"
    else
        log "push не прошёл (попробую в следующий раз): ${push_out:-без вывода}"
    fi
fi

# Лог не должен расти вечно
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
