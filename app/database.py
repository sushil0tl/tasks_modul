"""Слой доступа к данным: таблицы SQLAlchemy Core + пул соединений с PostgreSQL.

Репозиторий работает напрямую с SQL (без ORM), поэтому этот модуль отвечает только за:
 * описание таблиц (для генерации DDL и типовизированных запросов);
 * создание/выдачу соединений;
 * применение схемы (``db/schema.sql``) при старте.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool
from sqlalchemy.dialects import postgresql
from app.config import get_settings
from app.models import TicketStage

#: порядок стадий (используется при описании ENUM-типа)
STAGE_VALUES = [stage.value for stage in TicketStage]

logger = logging.getLogger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

#: все таблицы сервиса живут в одной схеме БД (public), но в отдельном namespace метаданных
metadata = MetaData()

#: PostgreSQL ENUM ticket_stage: значения берутся из app.models.TicketStage,
#: сам тип в БД создаёт db/schema.sql (create_type=False).
TICKET_STAGE_ENUM = postgresql.ENUM(
    *STAGE_VALUES,
    name="ticket_stage",
    create_type=False,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
)

# --- Заявки (tasks) ----------------------------------------------------------
tickets = Table(
    "tickets",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("title", String(255), nullable=False),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("object_id", String(64), nullable=False),
    Column("author_id", String(64), nullable=False),
    Column("assignee_ids", Text, nullable=False, server_default=text("'[]'")),
    Column("watcher_ids", Text, nullable=False, server_default=text("'[]'")),
    # значения перечисления берём из TicketStage; тип в БД создаёт db/schema.sql (create_type=False)
    Column("stage", TICKET_STAGE_ENUM, nullable=False),
    Column("due_date", Date, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("closed_at", DateTime(timezone=True), nullable=True),
    Column("warning_source", String(128), nullable=True),
    Column("created_by", String(64), nullable=False),
    Column("is_deleted", Integer, nullable=False, server_default=text("0")),
    # бизнес-инварианты уровня хранилища (дублируют валидацию сервиса):
    # заявка всегда привязана к объекту и имеет название
    CheckConstraint("length(btrim(title)) >= 3", name="ck_tickets_title"),
    CheckConstraint("length(btrim(object_id)) >= 1", name="ck_tickets_object_id"),
    # дата исполнения не может быть раньше даты постановки
    CheckConstraint(
        "due_date IS NULL OR due_date >= created_at::date",
        name="ck_tickets_due_not_before_created",
    ),
)

# --- Справочник пользователей (кэш данных модуля прав доступа) ---------------
ticket_users = Table(
    "ticket_users",
    metadata,
    Column("user_id", String(64), primary_key=True),
    Column("full_name", String(255), nullable=False),
    Column("role", String(32), nullable=False),
    Column("position", String(128), nullable=True),
    Column("email", String(255), nullable=True),
    Column("is_active", Integer, nullable=False, server_default=text("1")),
)

# --- История изменений (аудит) ----------------------------------------------
ticket_history = Table(
    "ticket_history",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ticket_id", Integer, nullable=False),
    Column("changed_by", String(64), nullable=False),
    Column("action", String(32), nullable=False),
    Column("field", String(64), nullable=True),
    Column("old_value", Text, nullable=True),
    Column("new_value", Text, nullable=True),
    Column("comment", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# --- Журнал уведомлений (исполнителям/наблюдателям) -------------------------
ticket_notifications = Table(
    "ticket_notifications",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("ticket_id", Integer, nullable=False),
    Column("recipient", String(64), nullable=False),
    Column("event_type", String(32), nullable=False),
    Column("subject", String(255), nullable=False),
    Column("body", Text, nullable=False, server_default=text("''")),
    Column("channel", String(32), nullable=False, server_default=text("'inbox'")),
    Column("status", String(16), nullable=False, server_default=text("'sent'")),
    Column("error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


class Database:
    """Обёртка над движком PostgreSQL (пул соединений + применение схемы)."""

    def __init__(self, url: Optional[str] = None) -> None:
        settings = get_settings()
        self.url = url or settings.database_url
        self._engine: Optional[Engine] = None

    # ------------------------------------------------------------------ engine
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_engine(
                self.url,
                poolclass=QueuePool,
                pool_size=get_settings().db_pool_size,
                max_overflow=get_settings().db_max_overflow,
                pool_recycle=get_settings().db_pool_recycle,
                pool_pre_ping=True,
                future=True,
            )
            logger.info("Database engine created (%s)", _mask_url(self.url))
        return self._engine

    def connect(self):
        """Новое соединение из пула (context manager)."""
        return self.engine.connect()

    # ------------------------------------------------------------------- admin
    def apply_schema(self) -> None:
        """Создать таблицы, если их ещё нет (идемпотентно).

        Приоритет - ``db/schema.sql`` (он же содержит ENUM-тип ticket_stage);
        если файла нет - генерируем DDL из метаданных.
        """
        if SCHEMA_FILE.exists():
            with self.engine.begin() as conn:
                conn.execute(text(SCHEMA_FILE.read_text(encoding="utf-8")))
            logger.info("Schema applied from %s", SCHEMA_FILE.name)
        else:  # pragma: no cover - резервный путь
            metadata.create_all(self.engine, checkfirst=True)
            logger.info("Schema created from SQLAlchemy metadata")

    def ping(self) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:  # pragma: no cover - зависит от окружения
            logger.exception("Database ping failed")
            return False

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None


def _mask_url(url: str) -> str:
    """URL без пароля - для логов."""
    try:
        if "@" in url and "://" in url:
            scheme, rest = url.split("://", 1)
            creds, host = rest.rsplit("@", 1)
            user = creds.split(":", 1)[0]
            return f"{scheme}://{user}:***@{host}"
    except Exception:
        pass
    return "***"


#: единственный экземпляр на процесс (инициализируется лениво)
db = Database()
