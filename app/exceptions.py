"""Доменные исключения сервиса заявок.

Каждое исключение знает, в какой HTTP-код оно должно превратиться на границе API,
поэтому обработчики роутеров остаются чистыми (см. ``app.main.register_exception_handlers``).
"""

from __future__ import annotations

from typing import Any


class TicketServiceError(Exception):
    """Базовое исключение сервиса."""

    http_status: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class ValidationError(TicketServiceError):
    """Некорректные данные запроса или нарушение бизнес-правила."""

    http_status = 400
    code = "validation_error"


class StageTransitionError(ValidationError):
    """Запрошен недопустимый переход стадии заявки."""

    code = "invalid_stage_transition"


class NotFoundError(TicketServiceError):
    """Объект не найден (или недоступен субъекту - тогда отвечаем 404, а не 403)."""

    http_status = 404
    code = "not_found"


class ConflictError(TicketServiceError):
    """Конфликт состояния (например, удаление заявки в активной стадии)."""

    http_status = 409
    code = "conflict"


class AuthenticationError(TicketServiceError):
    """Субъект не опознан."""

    http_status = 401
    code = "unauthenticated"


class PermissionDeniedError(TicketServiceError):
    """Модуль прав доступа запретил действие."""

    http_status = 403
    code = "permission_denied"


class AccessControlUnavailableError(TicketServiceError):
    """Модуль прав доступа недоступен - безопаснее отказать, чем разрешить."""

    http_status = 503
    code = "access_control_unavailable"


class StorageError(TicketServiceError):
    """Ошибка хранилища (PostgreSQL): соединение, целостность, миграции."""

    http_status = 503
    code = "storage_error"


class IntegrityConflictError(ConflictError):
    """Нарушение ограничений целостности БД (уникальность, внешний ключ)."""

    code = "integrity_conflict"


__all__ = [
    "TicketServiceError",
    "ValidationError",
    "StageTransitionError",
    "NotFoundError",
    "ConflictError",
    "AuthenticationError",
    "PermissionDeniedError",
    "AccessControlUnavailableError",
    "StorageError",
    "IntegrityConflictError",
]
