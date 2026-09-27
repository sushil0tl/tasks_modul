"""REST API пользователей (справочник для выбора исполнителя/наблюдателя).

Данные о ролях и полномочиях принадлежат **модулю прав доступа**; сервис заявок
использует их только для отображения ФИО в карточках и проверки участников.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_actor, get_ticket_service
from app.models import ActorContext, Role, UserDTO, UserInfoResponse
from app.services.ticket_service import TicketService

router = APIRouter(prefix="/users", tags=["Пользователи"])


@router.get("/me", response_model=UserInfoResponse, summary="Текущий субъект и его права")
def whoami(actor: ActorContext = Depends(get_actor), service: TicketService = Depends(get_ticket_service)) -> UserInfoResponse:
    return service.whoami(actor)


@router.get("", response_model=List[UserDTO], summary="Справочник сотрудников")
def list_users(
    role: Optional[Role] = None,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> List[UserDTO]:
    """Кого можно назначить исполнителем или наблюдателем."""
    return service.list_users(actor, role=role)
