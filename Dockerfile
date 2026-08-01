FROM python:3.12-slim

# node нужен самому движку: Agent SDK работает поверх claude CLI.
#
# Версия движка ЗАПИНЕНА намеренно. Без пина один и тот же коммит собирал бота
# с разным мозгом: 30.07.2026 пересборка ради зрения молча привезла движок,
# умеющий фоновых агентов, коуч тут же одного нанял — и бот дважды ответил
# заглушкой (инцидент 2026-07-30-coach-bot-заглушка-вместо-ответа.md).
# Поднимать версию — отдельным осознанным коммитом, не побочным эффектом сборки.
ARG CLAUDE_CODE_VERSION=2.1.216
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg openssh-client \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Конституции коуча в образе нет намеренно: она уехала в плагин gtd-coach
# и приезжает смонтированным клоном (том /plugin в compose). Правка текста
# правила не должна требовать пересборки питона.
COPY src/ ./src/
# Разовые замерщики. В образ они попадают ради того, чтобы замер повторялся:
# число, которое нельзя перепроверить командой, — это воспоминание о числе,
# а не число. Боту они не мешают: запускаются руками, сами не стартуют.
COPY scripts/ ./scripts/

# Каталоги создаём до монтирования: docker переносит их владельца на новый том
RUN useradd -u 1000 -m coach && mkdir -p /brain /state /archive \
    && chown -R coach /app /brain /state /archive
USER coach

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/home/coach

CMD ["python", "-m", "src.main"]
