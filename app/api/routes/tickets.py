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
    description=(
        "Справочник жизненного цикла: `code` (латинский код стадии), `title` (русское название) "
        "и `next_stages` — куда можно перейти из текущей стадии. Маршрут:\n\n"
        "`Не обработана -> Ожидает ТО -> Диагностика -> В работе -> Контроль -> Обработана`\n\n"
        "Возврат на шаг назад разрешён (переделка/уточнение); «прыжок» через стадию — "
        "только при включённой настройке ALLOW_STAGE_SKIP."
    ),
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
        "Создаёт заявку по предупреждению, пришедшему с объекта (форма менеджера).\n\n"
        "**Обязательные поля:** `title` (3–255 символов) и `object_id` — привязка к объекту.\n\n"
        "**Валидация дат** (`due_date`, `created_at`):\n"
        "* дата постановки не может быть в будущем (допуск +5 мин на часы клиента);\n"
        "* срок исполнения не раньше даты постановки;\n"
        "* для заявок «сегодняшнего дня» срок не может быть в прошлом;\n"
        "* горизонт планирования — максимум 5 лет вперёд.\n\n"
        "**Участники:** `assignee_ids` и `watcher_ids` — списки user_id из справочника "
        "сотрудников (`GET /api/v1/users`); один сотрудник не может быть одновременно "
        "исполнителем и наблюдателем; постановщик не может быть исполнителем своей заявки. "
        "`author_id` по умолчанию = текущий пользователь.\n\n"
        "**Стадия:** новая заявка всегда создаётся как `UNPROCESSED` «Не обработана» "
        "(исключение — администратор). Смена стадии — отдельный эндпоинт "
        "`PUT /tickets/{id}/stage`.\n\n"
        "**Права:** действие `ticket.create` проверяется модулем прав доступа; "
        "менеджер может создавать заявки только по своим объектам.\n\n"
        "**Побочные эффекты:** запись в историю изменений (audit), уведомления "
        "исполнителям и наблюдателям, инвалидация кэша.\n\n"
        "Ответ: 201 + карточка заявки; ошибки: 400 (бизнес-валидация), 401/403 (права), "
        "404 (неизвестный сотрудник), 422 (схема), 409/503 (хранилище)."
    ),
    responses={
        201: {
            "description": "Заявка создана",
            "content": {
                "application/json": {"example": {
                    "id": 1,
                    "title": "Плановое ТО насоса ЦНС-180",
                    "description": "По предупреждению SCADA: вибрация подшипника 7.2 мм/с",
                    "object_id": "OBJ-101",
                    "due_date": "2026-10-15",
                    "warning_source": "SCADA/WARN-2026-09-29-014",
                    "stage": "UNPROCESSED",
                    "stage_title": "Не обработана",
                    "next_stages": ["PENDING_MAINT"],
                    "author": {"user_id": "manager_ivanov", "full_name": "Иванов Иван Иванович", "role": "MANAGER"},
                    "assignees": [{"user_id": "engineer_kuznetsov", "full_name": "Кузнецов Пётр Олегович", "role": "ENGINEER"}],
                    "watchers": [{"user_id": "manager_petrova", "full_name": "Петрова Анна Сергеевна", "role": "MANAGER"}],
                    "created_at": "2026-09-29T10:00:00Z",
                    "updated_at": "2026-09-29T10:00:00Z",
                    "closed_at": None,
                    "created_by": "manager_ivanov",
                }}
            },
        },
        400: {"description": "Нарушение бизнес-правил (стадия, состав участников, согласование дат)"},
        403: {"description": "Модуль прав доступа запретил создание заявки"},
        404: {"description": "Указанный исполнитель/наблюдатель/постановщик не найден в справочнике"},
    },
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
    description=(
        "Поиск заявок с учётом прав: **ADMIN** видит все заявки; **MANAGER/ENGINEER/OBSERVER** — "
        "только заявки своих объектов и те, где они являются постановщиком/исполнителем/наблюдателем. "
        "Результат кэшируется на короткий срок (CACHE_LIST_TTL)."
    ),
    responses={200: {"description": "Страница заявок"}, 403: {"description": "Действие ticket.read запрещено"}},
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
    description=(
        "Количество заявок в каждой стадии (в пределах видимости пользователя) + число "
        "просроченных. Опционально — в разрезе одного объекта (`object_id`). Кэшируется."
    ),
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
@router.get(
    "/{ticket_id}",
    response_model=TicketDTO,
    summary="Карточка заявки",
    description=(
        "Полная карточка: поля, стадия и допустимые переходы (`next_stages`), участники "
        "(постановщик, исполнители, наблюдатели с ФИО). Читается из кэша при наличии "
        "(CACHE_TICKET_TTL); любое изменение заявки инвалидирует запись.\n\n"
        "Доступ: администратор — любая заявка; остальные — свои объекты или участие в заявке."
    ),
    responses={200: {"description": "Карточка заявки"}, 403: {"description": "Нет прав на чтение заявки"}},
)
@router.patch(
    "/{ticket_id}",
    response_model=TicketDTO,
    summary="Редактировать заявку (PATCH)",
    description=(
        "Частичное обновление полей: `title`, `description`, `due_date`, `author_id`, "
        "`warning_source`, состав `assignee_ids`/`watcher_ids`. Стадия здесь не меняется — "
        "для неё `PUT /tickets/{id}/stage`.\n\n"
        "**Права:** MANAGER (постановщик/наблюдатель) и ADMIN; ENGINEER поля не правит.\n"
        "**Валидация:** итоговое состояние проверяется целиком (срок >= дата постановки, "
        "хотя бы один исполнитель, непересечение ролей участников).\n"
        "**Побочные эффекты:** история изменений (audit-log), уведомления участникам, "
        "инвалидация кэша. Поле `comment` попадёт в историю как пояснение правки."
    ),
    responses={
        200: {"description": "Заявка обновлена (новая карточка)"},
        400: {"description": "Нарушение бизнес-правил итогового состояния"},
        403: {"description": "Роль не может редактировать заявку"},
        404: {"description": "Заявка не найдена"},
    },
)
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
