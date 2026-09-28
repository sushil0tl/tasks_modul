"""Централизованное логирование микросервиса заявок.

Реализовано три канала:

* ``console``   - человекочитаемый (или JSON при ``LOG_JSON=1``) вывод в stdout;
* ``file``      - ротируемый файл ``LOG_FILE`` (``RotatingFileHandler``);
* ``audit``     - отдельный журнал аудита (``app.audit``): каждое изменение
  заявки дублируется сюда в формате JSON-строки, что удобно для внешней
  системы сбора логов (ELK/Loki). Файл ``logs/audit.log``.

Функция :func:`setup_logging` идемпотентна: повторные вызовы не плодят хендлеры.
Корневой логгер SQLAlchemy по умолчанию поднят в WARNING, чтобы SQL-шум не
затенял прикладные сообщения (управляется ``SQL_ECHO``).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Dict, Optional

#: имя логгера журнала аудита (отдельный файл + propagate=False)
AUDIT_LOGGER_NAME = "app.audit"

_CONFIGURED = False
_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_RESERVED = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
}


class JsonFormatter(logging.Formatter):
    """Превращает запись лога в одну JSON-строку (machine-readable логи)."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        payload: Dict[str, Any] = {
            "ts": self.formatTime(record, _DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class AuditContextFilter(logging.Filter):
    """Гарантирует наличие структурных полей у записей audit-логгера."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        for attr, default in (("ticket_id", None), ("actor", None), ("event", None)):
            if not hasattr(record, attr):
                setattr(record, attr, default)
        return True


def _build_console_handler(level: int, as_json: bool) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(
        JsonFormatter() if as_json else logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT)
    )
    return handler


def _build_file_handler(level: int, as_json: bool, log_file: str,
                        max_bytes: int, backup_count: int) -> Optional[logging.Handler]:
    if not log_file:
        return None
    path = Path(log_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
    except OSError:  # pragma: no cover - недоступная ФС не должна валить сервис
        logging.getLogger(__name__).warning("Не удалось открыть файл логов %s", log_file)
        return None
    handler.setLevel(level)
    handler.setFormatter(
        JsonFormatter() if as_json else logging.Formatter(_CONSOLE_FORMAT, _DATE_FORMAT)
    )
    return handler


def setup_logging(settings: Any = None, force: bool = False) -> logging.Logger:
    """Настроить логирование сервиса. Возвращает корневой логгер приложения."""
    global _CONFIGURED

    if settings is None:
        from app.config import get_settings  # локальный импорт против циклов

        settings = get_settings()

    root = logging.getLogger()
    if _CONFIGURED and not force:
        return logging.getLogger("ticket_service")

    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)

    # сбрасываем собственные хендлеры (не трогаем чужие, например pytest's)
    for handler in list(root.handlers):
        if getattr(handler, "_ticket_service", False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(min(level, logging.INFO))

    console = _build_console_handler(level, settings.log_json)
    console._ticket_service = True  # type: ignore[attr-defined]
    root.addHandler(console)

    file_handler = _build_file_handler(
        level, settings.log_json, settings.log_file, settings.log_max_bytes, settings.backup_count
    )
    if file_handler is not None:
        file_handler._ticket_service = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    # --- журнал аудита -------------------------------------------------------
    audit = logging.getLogger(AUDIT_LOGGER_NAME)
    audit.setLevel(logging.INFO)
    audit.propagate = False
    for handler in list(audit.handlers):
        if getattr(handler, "_ticket_audit", False):
            audit.removeHandler(handler)
            handler.close()

    if getattr(settings, "audit_log_enabled", True):
        audit_path = Path(settings.log_file or "logs/ticket_service.log").parent / "audit.log"
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            audit_handler: logging.Handler = logging.handlers.RotatingFileHandler(
                str(audit_path),
                maxBytes=settings.log_max_bytes,
                backupCount=settings.backup_count,
                encoding="utf-8",
            )
            audit_handler.setFormatter(JsonFormatter())
            audit_handler.addFilter(AuditContextFilter())
            audit_handler._ticket_audit = True  # type: ignore[attr-defined]
            audit.addHandler(audit_handler)
        except OSError:  # pragma: no cover
            audit.disabled = True

    # шум от драйверов БД/HTTP не должен затенять прикладные сообщения
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if settings.sql_echo else logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)

    _CONFIGURED = True
    logger = logging.getLogger("ticket_service")
    logger.info(
        "Logging configured: level=%s file=%s json=%s audit=%s",
        settings.log_level,
        settings.log_file or "-",
        settings.log_json,
        getattr(settings, "audit_log_enabled", True),
    )
    return logger


def get_audit_logger() -> logging.Logger:
    """Логгер журнала аудита (файл ``logs/audit.log``, JSON-строки)."""
    return logging.getLogger(AUDIT_LOGGER_NAME)


def audit_event(event: str, *, ticket_id: Optional[int] = None, actor: Optional[str] = None,
                **fields: Any) -> None:
    """Записать структурированное событие аудита.

    Пример записи::

        {"ts": "...", "level": "INFO", "logger": "app.audit", "event": "STAGE_CHANGED",
         "ticket_id": 1, "actor": "manager1", "from": "UNPROCESSED", "to": "PENDING_MAINT"}
    """
    payload: Dict[str, Any] = {"event": event, "ticket_id": ticket_id, "actor": actor}
    payload.update({k: v for k, v in fields.items() if v is not None})
    get_audit_logger().info(
        "audit %s",
        event,
        extra={"ticket_id": ticket_id, "actor": actor, "event": event, **payload},
    )


__all__ = ["setup_logging", "get_audit_logger", "audit_event", "AUDIT_LOGGER_NAME"]
