"""Настройки микросервиса заявок.

Все параметры читаются из переменных окружения (можно использовать .env),
по умолчанию заданы значения для локальной разработки.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация сервиса (ENV > .env > значения по умолчанию).

    Для исторических переменных окружения с префиксом ``TICKETS_``
    (``TICKETS_DATABASE_URL``, ``TICKETS_API_KEY``, ``TICKETS_HOST``,
    ``TICKETS_PORT``) добавлены алиасы — обе формы читаются одинаково.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Идентификация сервиса ------------------------------------------------
    app_name: str = "Ticket Service"
    app_version: str = "1.0.0"
    api_prefix: str = "/api/v1"

    # --- HTTP-сервер ---------------------------------------------------------
    tickets_host: str = Field(
        default="0.0.0.0",
        validation_alias=AliasChoices("TICKETS_HOST", "tickets_host"),
    )
    tickets_port: int = Field(
        default=9090,
        validation_alias=AliasChoices("TICKETS_PORT", "tickets_port"),
    )

    # --- Хранилище ------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+psycopg2://tickets_app:tickets_pass@127.0.0.1:5433/tickets_db",
        validation_alias=AliasChoices("TICKETS_DATABASE_URL", "database_url"),
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800
    auto_create_schema: bool = True
    seed_demo_data: bool = True

    # --- Ключи ----------------------------------------------------------------
    #: ключ вызывающей стороны (API-шлюз / фронтенд) для обращений к этому API
    tickets_api_key: str = Field(
        default="gateway-secret-key",
        validation_alias=AliasChoices("TICKETS_API_KEY", "tickets_api_key"),
    )
    #: ключ, которым сервис подписывает запросы в модуль прав доступа
    access_control_api_key: str = "service-secret-key"

    # --- Модуль прав доступа --------------------------------------------------
    #: пустая строка => используется встроенный InMemoryAccessControlClient (dev)
    access_control_url: str = ""
    access_control_timeout: float = 3.0

    # --- Бизнес-правила -------------------------------------------------------
    #: разрешены ли "прыжки" через стадии (не только по маршруту)
    allow_stage_skip: bool = False
    #: сколько записей возвращать по умолчанию
    default_page_size: int = 20
    max_page_size: int = 200

    # --- Логирование ----------------------------------------------------------
    log_level: str = "INFO"
    #: путь к файлу логов (пусто -> только stdout)
    log_file: str = "logs/ticket_service.log"
    #: размер файла до ротации, байт (10 MiB)
    log_max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    #: писать JSON-строки вместо текстового формата
    log_json: bool = False
    #: включать запросы SQLAlchemy в общий лог (по умолчанию - нет, только WARNING+)
    sql_echo: bool = False

    # --- Кеширование -----------------------------------------------------------
    cache_enabled: bool = True
    #: TTL кэша карточек заявок, сек
    cache_ticket_ttl: int = 60
    #: TTL кэша списков/статистики, сек
    cache_list_ttl: int = 15
    #: TTL кэша истории изменений, сек
    cache_history_ttl: int = 30
    #: максимум записей в LRU-кэше процесса
    cache_max_entries: int = 1024
    #: Redis для распределённого кэша (пусто -> только in-process кэш)
    redis_url: str = ""
    redis_cache_prefix: str = "tickets"

    # --- Аудит -------------------------------------------------------------------
    #: писать audit log (историю изменений) в отдельный файл app.audit
    audit_log_enabled: bool = True

    # --- Уведомления --------------------------------------------------------------
    notifications_enabled: bool = True
    #: уведомлять постановщика о смене стадии / комментарии
    notify_author: bool = True
    #: webhook для отправки уведомлений (пусто -> консоль + журнал памяти)
    notification_webhook_url: str = ""
    notification_timeout: float = 3.0
    #: сколько последних уведомлений держать в памяти для /notifications
    notification_inbox_size: int = 500


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Кэш настроек процесса."""
    return Settings()
