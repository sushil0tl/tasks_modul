"""FastAPI-зависимости аутентификации/авторизации.

Сервис ожидает, что API-шлюз (или модуль аутентификации) уже проверил токен и передал:
    ``X-API-Key``  - ключ вызывающей системы;
    ``X-User-Id``  - субъект, от имени которого выполняется запрос;
    ``X-Role``     - роль субъекта (подсказка, сверяется с модулем прав доступа).

Далее сервис **не принимает решений о правах сам** - он запрашивает их у модуля
прав доступа (:mod:`app.auth.access_control`).
"""

from __future__ import annotations

import secrets
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException, Request

from app.auth.access_control import AccessControlClient, get_access_control_client
from app.config import get_settings
from app.exceptions import TicketServiceError
from app.models import ActionType, ActorContext


def _to_http(exc: Exception) -> HTTPException:
    """Доменное исключение -> HTTP-ответ."""
    if isinstance(exc, TicketServiceError):
        return HTTPException(status_code=exc.http_status, detail=exc.message)
    return exc  # type: ignore[return-value]


def verify_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """Проверка ключа вызывающей стороны (межсервисная аутентификация)."""
    expected = get_settings().tickets_api_key
    if not expected:
        return  # ключ не настроен - режим разработки
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Некорректный или отсутствующий X-API-Key")


def get_access_control() -> AccessControlClient:
    """Клиент модуля прав доступа (инъекция в роутеры/сервис)."""
    return get_access_control_client()


def get_actor(
    x_user_id: Optional[str] = Header(default=None, alias="X-User-Id"),
    x_role: Optional[str] = Header(default=None, alias="X-Role"),
    _: None = Depends(verify_api_key),
    access: AccessControlClient = Depends(get_access_control),
) -> ActorContext:
    """Субъект запроса + его права (получены из модуля прав доступа)."""
    if not x_user_id:
        raise HTTPException(
            status_code=401,
            detail="Заголовок X-User-Id обязателен: сервис работает от имени конкретного пользователя",
        )
    try:
        return access.get_actor(x_user_id.strip(), hint_role=x_role)
    except TicketServiceError as exc:
        raise _to_http(exc) from exc


def require_action(action: ActionType) -> Callable[..., ActorContext]:
    """Фабрика зависимостей: требует у модуля прав доступа разрешение ``action``."""

    def dependency(
        actor: ActorContext = Depends(get_actor),
        access: AccessControlClient = Depends(get_access_control),
    ) -> ActorContext:
        try:
            access.require(actor.user_id, action)
        except TicketServiceError as exc:
            raise _to_http(exc) from exc
        return actor

    dependency.__name__ = f"require_{action.name.lower()}"
    return dependency


def get_ticket_service(request: Request):
    """Бизнес-сервис заявок, собранный при старте приложения."""
    service = getattr(request.app.state, "ticket_service", None)
    if service is None:  # pragma: no cover - защита от неверного порядка инициализации
        raise HTTPException(status_code=503, detail="Сервис заявок не инициализирован")
    return service
