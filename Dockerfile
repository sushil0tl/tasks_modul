# syntax=docker/dockerfile:1
# ============================================================================
# Микросервис заявок (Ticket Service) — образ для продакшена.
#
# Multi-stage сборка: зависимости ставятся в отдельном слое, в финальный
# образ попадают только установленные пакеты и код приложения.
#
# Сборка:  docker build -t ticket-service:1.0.0 .
# Запуск:  docker run -d --name ticket-service -p 9090:9090 \
#            -e TICKETS_DATABASE_URL=postgresql+psycopg2://tickets_app:tickets_pass@db:5433/tickets_db \
#            ticket-service:1.0.0
# ============================================================================

# --------------------------------------------------------------------------
# Стадия сборки зависимостей
# --------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# --------------------------------------------------------------------------
# Финальный образ
# --------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="ticket-service" \
      org.opencontainers.image.description="Микросервис управления заявками (CRM-задачи): FastAPI + PostgreSQL" \
      org.opencontainers.image.version="1.0.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    # host/port HTTP-сервера внутри контейнера (переопределяются через -e)
    TICKETS_HOST=0.0.0.0 \
    TICKETS_PORT=9090

# Только минимально необходимые системные пакеты:
# curl — healthcheck, libpq5 — драйвер psycopg2-binary
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Непривилегированный пользователь (требование безопасности)
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --shell /usr/sbin/nologin --create-home appuser

WORKDIR /srv/ticket-service

# Виртуальное окружение с зависимостями из стадии сборки
COPY --from=builder /opt/venv /opt/venv

# Код приложения и схема БД
COPY app/ ./app/
COPY db/ ./db/
COPY run.py README.md ./

# Каталог логов доступен на запись непривилегированному пользователю
RUN mkdir -p logs && chown -R appuser:appuser /srv/ticket-service

USER appuser

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:9090/health || exit 1

# Uvicorn поднимает несколько воркеров; graceful shutdown за 25 секунд
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9090", "--workers", "2"]
