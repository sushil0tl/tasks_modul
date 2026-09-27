"""Служебный REST API кеширования.

* ``GET  /cache/stats`` — состояние кэша (бэкенд, hit/miss, число записей);
  доступна любому аутентифицированному пользователю (только метрики, без данных).
* ``POST /cache/clear``  — полный сброс кэша; **только ADMIN** (например, после
  миграции схемы или ручного вмешательства в БД).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_actor, get_ticket_service
from app.exceptions import PermissionDeniedError
from app.models import ActorContext, CacheStatsResponse, MessageResponse, Role
from app.services.ticket_service import TicketService

router = APIRouter(prefix="/cache", tags=["Кеширование"])


@router.get(
    "/stats",
    response_model=CacheStatsResponse,
    summary="Статистика кэша",
)
def cache_stats(
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> CacheStatsResponse:
    return CacheStatsResponse(**service.cache_stats())


@router.post(
    "/clear",
    response_model=MessageResponse,
    summary="Сбросить кэш (только администратор)",
)
def cache_clear(
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> MessageResponse:
    if not actor.sees_everything or not actor.has_role(Role.ADMIN):
        raise PermissionDeniedError(
            "Полный сброс кэша доступен только администратору", role=actor.role.value
        )
    result = service.cache_clear()
    return MessageResponse(message="Кэш очищен", details=result)
