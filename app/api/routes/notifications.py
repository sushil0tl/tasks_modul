"""REST API уведомлений исполнителям/наблюдателям.

Уведомления формируются автоматически при событиях жизненного цикла заявки
(создание, смена стадии, редактирование, комментарий, удаление) и сохраняются
в журнал ``ticket_notifications``. Здесь — только чтение журнала:

* ``GET /notifications``          — «ящик» текущего пользователя (mine=true),
  либо ящик другого пользователя (доступно ADMIN и MANAGER);
* ``GET /tickets/{id}/notifications`` — уведомления по конкретной заявке
  (требует права чтения заявки).

Права проверяет сервис (:meth:`TicketService.list_notifications`) через модуль
прав доступа; роутер остаётся тонким слоем.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, Query

from app.auth.dependencies import get_actor, get_ticket_service
from app.models import ActorContext, NotificationDTO
from app.services.ticket_service import TicketService

router = APIRouter(tags=["Уведомления"])


@router.get(
    "/notifications",
    response_model=List[NotificationDTO],
    summary="Мои уведомления (или уведомления сотрудника)",
    description=(
        "По умолчанию возвращает «ящик» текущего пользователя (заголовок X-User-Id). "
        "Параметр user_id позволяет администратору и менеджеру посмотреть уведомления "
        "другого сотрудника (менеджеру — только по его объектам)."
    ),
)
def list_my_notifications(
    user_id: str | None = Query(None, max_length=64, description="Чей ящик показать (ADMIN/MANAGER)"),
    ticket_id: int | None = Query(None, ge=1, description="Фильтр по заявке"),
    limit: int = Query(50, ge=1, le=200),
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> List[NotificationDTO]:
    rows = service.list_notifications(
        actor, user_id=user_id, ticket_id=ticket_id, mine=not user_id, limit=limit
    )
    return [NotificationDTO(**row) for row in rows]


@router.get(
    "/tickets/{ticket_id}/notifications",
    response_model=List[NotificationDTO],
    summary="Уведомления по заявке",
)
def list_ticket_notifications(
    ticket_id: int,
    limit: int = Query(100, ge=1, le=500),
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> List[NotificationDTO]:
    rows = service.list_notifications(actor, ticket_id=ticket_id, limit=limit)
    return [NotificationDTO(**row) for row in rows]
