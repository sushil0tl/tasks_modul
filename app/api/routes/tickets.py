"""REST API заявок.

Тонкий слой: разбирает запрос, отдаёт контекст субъекта (из модуля прав доступа),
вызывает соответствующий метод ``TicketService`` и сериализует ответ.
Логика и проверки прав - в сервисе, SQL - в репозитории.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request, status

from app.auth.dependencies import get_actor, get_access_control, get_ticket_service
from app.auth.access_control import AccessControlClient
from app.models import (
    ActionType,
    ActorContext,
    CommentRequest,
    HistoryEntryDTO,
    MessageResponse,
    StageChangeRequest,
    StageInfo,
    StageStatsResponse,
    TicketCreateRequest,
    TicketDTO,
    TicketListResponse,
    TicketStage,
    TicketUpdateRequest,
)
from app.services.ticket_service import TicketService

router = APIRouter(prefix="/tickets", tags=["Заявки"])


# =============================================================================
# Справочное: стадии жизненного цикла
# =============================================================================
@router.get(
    "/stages",
    response_model=List[StageInfo],
    summary="Стадии заявки и допустимые переходы",
    dependencies=[Depends(get_actor)],
)
def list_stages() -> List[StageInfo]:
    """Маршрут: Не обработана -> Ожидает ТО -> Диагностика -> В работе -> Контроль -> Обработана."""
    return [
        StageInfo(code=stage, title=stage.title_ru, next_stages=stage.next_stages)
        for stage in TicketStage
    ]


# =============================================================================
# Создание заявки (по предупреждению с объекта)
# =============================================================================
@router.post(
    "",
    response_model=TicketDTO,
    status_code=status.HTTP_201_CREATED,
    summary="Создать заявку",
    description=(
        "Менеджер заполняет форму по предупреждению объекта: привязка к object_id обязательна. "
        "Право на создание проверяется модулем прав доступа."
    ),
)
def create_ticket(
    payload: TicketCreateRequest,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> TicketDTO:
    return service.create_ticket(payload, actor)


# =============================================================================
# Список / поиск
# =============================================================================
@router.get(
    "",
    response_model=TicketListResponse,
    summary="Список заявок (фильтры + пагинация)",
)
def list_tickets(
    request: Request,
    stage: Optional[List[TicketStage]] = Query(None, description="Фильтр по стадии (можно несколько)"),
    object_id: Optional[str] = Query(None, max_length=64, description="Фильтр по объекту"),
    author_id: Optional[str] = Query(None, max_length=64, description="Постановщик"),
    assignee_id: Optional[str] = Query(None, max_length=64, description="Исполнитель"),
    watcher_id: Optional[str] = Query(None, max_length=64, description="Наблюдатель"),
    only_mine: bool = Query(False, description="Только заявки, где я участник"),
    overdue_only: bool = Query(False, description="Только просроченные (due_date < сегодня)"),
    search: Optional[str] = Query(None, min_length=2, max_length=128, description="Поиск по названию/описанию/объекту"),
    created_from: Optional[datetime] = Query(None, description="Дата постановки с"),
    created_to: Optional[datetime] = Query(None, description="Дата постановки по"),
    order_by: str = Query("created_at", pattern="^(created_at|updated_at|due_date|title|stage|id)$"),
    order_desc: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> TicketListResponse:
    return service.list_tickets(
        actor,
        stages=stage,
        object_id=object_id,
        author_id=author_id,
        assignee_id=assignee_id,
        watcher_id=watcher_id,
        only_mine=only_mine,
        overdue_only=overdue_only,
        search=search,
        created_from=created_from,
        created_to=created_to,
        order_by=order_by,
        order_desc=order_desc,
        page=page,
        page_size=page_size,
    )


# =============================================================================
# Статистика по стадиям
# =============================================================================
@router.get(
    "/stats/stages",
    response_model=StageStatsResponse,
    summary="Счётчики заявок по стадиям",
)
def stage_stats(
    object_id: Optional[str] = Query(None, max_length=64),
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> StageStatsResponse:
    return service.stage_stats(actor, object_id=object_id)


# =============================================================================
# Карточка / редактирование / удаление
# =============================================================================
@router.get("/{ticket_id}", response_model=TicketDTO, summary="Карточка заявки")
def get_ticket(
    ticket_id: int,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> TicketDTO:
    return service.get_ticket(ticket_id, actor)


@router.patch("/{ticket_id}", response_model=TicketDTO, summary="Редактировать заявку")
def update_ticket(
    ticket_id: int,
    payload: TicketUpdateRequest,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> TicketDTO:
    return service.update_ticket(ticket_id, payload, actor)


@router.delete("/{ticket_id}", response_model=MessageResponse, summary="Удалить заявку")
def delete_ticket(
    ticket_id: int,
    hard: bool = Query(False, description="true - физическое удаление (только администратор)"),
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> MessageResponse:
    result = service.delete_ticket(ticket_id, actor, hard=hard)
    return MessageResponse(
        message=f"Заявка {ticket_id} удалена ({result['mode']} removal)", details=result
    )


# =============================================================================
# Стадия
# =============================================================================
@router.put("/{ticket_id}/stage", response_model=TicketDTO, summary="Сменить стадию заявки")
def change_stage(
    ticket_id: int,
    payload: StageChangeRequest,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> TicketDTO:
    return service.change_stage(ticket_id, payload, actor)


# =============================================================================
# Комментарий / история
# =============================================================================
@router.post(
    "/{ticket_id}/comments",
    response_model=HistoryEntryDTO,
    status_code=status.HTTP_201_CREATED,
    summary="Добавить комментарий",
)
def add_comment(
    ticket_id: int,
    payload: CommentRequest,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> HistoryEntryDTO:
    return service.add_comment(ticket_id, payload.text, actor)


@router.get(
    "/{ticket_id}/history",
    response_model=List[HistoryEntryDTO],
    summary="История изменений заявки",
)
def get_history(
    ticket_id: int,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> List[HistoryEntryDTO]:
    return service.get_history(ticket_id, actor)


# =============================================================================
# Заявки объекта (дублирующий удобный путь)
# =============================================================================
@router.get("/by-object/{object_id}", response_model=TicketListResponse, summary="Заявки объекта")
def tickets_by_object(
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
