"""REST API по объектам: сводка и заявки конкретного объекта.

Объекты - это то, с чего приходят предупреждения; модуль заявок не ведёт реестр
объектов (это другой микросервис), а показывает только свою аналитику по ним.
Список доступных объектов отдаёт модуль прав доступа.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_actor, get_access_control
from app.auth.access_control import AccessControlClient
from app.models import ActionType, ActorContext, ObjectSummaryDTO, TicketListResponse, TicketStage
from app.services.ticket_service import TicketService
from app.auth.dependencies import get_ticket_service

router = APIRouter(prefix="/objects", tags=["Объекты"])


@router.get("", response_model=List[ObjectSummaryDTO], summary="Объекты со сводкой по заявкам")
def list_objects(
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> List[ObjectSummaryDTO]:
    return service.list_objects(actor)


@router.get("/{object_id}/tickets", response_model=TicketListResponse, summary="Заявки объекта")
def object_tickets(
    object_id: str,
    stage: Optional[List[TicketStage]] = Query(None),
    only_mine: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> TicketListResponse:
    return service.tickets_by_object(
        object_id, actor, stages=stage, only_mine=only_mine, page=page, page_size=page_size
    )


@router.get("/allowed", summary="Список объектов, доступных субъекту (из модуля прав доступа)")
def allowed_objects(
    actor: ActorContext = Depends(get_actor),
    access: AccessControlClient = Depends(get_access_control),
):
    access.require(actor.user_id, ActionType.OBJECT_READ)
    return {"user_id": actor.user_id, "objects": list(access.get_allowed_objects(actor.user_id))}
