FROM python:3.12-slim

# node нужен самому движку: Agent SDK работает поверх claude CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg openssh-client \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY prompts/ ./prompts/

# Каталоги создаём до монтирования: docker переносит их владельца на новый том
RUN useradd -u 1000 -m coach && mkdir -p /brain /state /archive \
    && chown -R coach /app /brain /state /archive
USER coach

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOME=/home/coach

CMD ["python", "-m", "src.main"]
