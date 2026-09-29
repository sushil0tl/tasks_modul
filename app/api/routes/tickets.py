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
# Общие примеры ответов для всех операций со заявками
# =============================================================================
TICKET_CARD_EXAMPLE = {
    "id": 1,
    "title": "Плановое ТО насоса ЦНС-180",
    "description": "По предупреждению SCADA: вибрация подшипника 7.2 мм/с, рост температуры корпуса",
    "object_id": "OBJ-101",
    "due_date": "2026-10-15",
    "warning_source": "SCADA/WARN-2026-09-29-014",
    "stage": "UNPROCESSED",
    "stage_title": "Не обработана",
    "next_stages": ["PENDING_MAINT"],
    "author": {"user_id": "manager_ivanov", "full_name": "Иванов Иван Иванович", "role": "MANAGER"},
    "assignees": [
        {"user_id": "engineer_kuznetsov", "full_name": "Кузнецов Пётр Олегович", "role": "ENGINEER"},
        {"user_id": "engineer_smirnov", "full_name": "Смирнов Олег Викторович", "role": "ENGINEER"},
    ],
    "watchers": [{"user_id": "manager_petrova", "full_name": "Петрова Анна Сергеевна", "role": "MANAGER"}],
    "created_at": "2026-09-29T09:57:31Z",
    "updated_at": "2026-09-29T09:57:31Z",
    "closed_at": None,
    "created_by": "manager_ivanov",
}

TICKET_LIST_EXAMPLE = {
    "total": 42,
    "page": 1,
    "page_size": 20,
    "pages": 3,
    "items": [TICKET_CARD_EXAMPLE],
}

HISTORY_ENTRY_EXAMPLE = {
    "id": 12,
    "ticket_id": 1,
    "action": "STAGE_CHANGED",
    "field": "stage",
    "old_value": "UNPROCESSED",
    "new_value": "PENDING_MAINT",
    "comment": "Передано в плановое ТО, согласовано с диспетчером",
    "changed_by": {"user_id": "manager_ivanov", "full_name": "Иванов Иван Иванович", "role": "MANAGER"},
    "created_at": "2026-09-29T12:03:11Z",
}


def _error_example(code: str, message: str, **details) -> dict:
    """Пример тела ошибки в едином формате сервиса (для блоков responses в Swagger)."""
    return {
        "error": code,
        "message": message,
        "details": details,
        "path": "/api/v1/tickets/1",
        "request_id": "ab12cd34ef56",
    }


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
        "только при включённой настройке ALLOW_STAGE_SKIP.\n\n"
        "**Что передавать:** тело запроса не нужно; только заголовки авторизации "
        "(`X-API-Key`, `X-User-Id`, `X-Role`).\n\n"
        "**Что приходить:** массив из 6 объектов `{code, title, next_stages}`."
    ),
    responses={
        200: {
            "description": "Список стадий по порядку жизненного цикла",
            "content": {"application/json": {"example": [
                {"code": "UNPROCESSED", "title": "Не обработана", "next_stages": ["PENDING_MAINT"]},
                {"code": "PENDING_MAINT", "title": "Ожидает ТО", "next_stages": ["UNPROCESSED", "DIAGNOSTICS"]},
                {"code": "DIAGNOSTICS", "title": "Диагностика", "next_stages": ["PENDING_MAINT", "IN_PROGRESS"]},
                {"code": "IN_PROGRESS", "title": "В работе", "next_stages": ["DIAGNOSTICS", "CONTROL"]},
                {"code": "CONTROL", "title": "Контроль", "next_stages": ["IN_PROGRESS", "PROCESSED"]},
                {"code": "PROCESSED", "title": "Обработана", "next_stages": ["CONTROL"]},
            ]}},
        },
    },
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
        "## Что подавать на вход\n\n"
        "**Заголовки (обязательны для всех запросов к `/api/v1/*`):**\n"
        "| Заголовок | Обязательно | Значение |\n"
        "|---|---|---|\n"
        "| `X-API-Key` | да* | ключ вызывающей системы (`TICKETS_API_KEY`; *если не настроен — проверка отключена) |\n"
        "| `X-User-Id` | да | ID пользователя от имени которого запрос, напр. `manager_ivanov` |\n"
        "| `X-Role` | нет | подсказка роли: `ADMIN` / `MANAGER` / `ENGINEER` / `OBSERVER` |\n\n"
        "**Тело запроса (JSON)** — схема `TicketCreateRequest`:\n\n"
        "| Поле | Тип | Обязательно | Что передавать |\n"
        "|---|---|---|---|\n"
        "| `title` | string | **ДА** | название заявки, 3–255 символов |\n"
        "| `object_id` | string | **ДА** | ID объекта-источника предупреждения (`GET /api/v1/objects/allowed`) |\n"
        "| `description` | string | нет | описание проблемы, до 8000 символов |\n"
        "| `due_date` | date | нет | срок `ГГГГ-ММ-ДД`: не раньше даты постановки, не дальше 5 лет |\n"
        "| `warning_source` | string | нет | код/источник предупреждения, до 128 символов |\n"
        "| `author_id` | string | нет | постановщик; **по умолчанию — текущий `X-User-Id`** |\n"
        "| `created_at` | date-time | нет | дата постановки ISO-8601; **по умолчанию — сейчас**; будущее запрещено (+5 мин допуск) |\n"
        "| `assignee_ids` | string[] | нет | user_id исполнителей (`GET /api/v1/users`); можно несколько |\n"
        "| `watcher_ids` | string[] | нет | user_id наблюдателей; пересечение с `assignee_ids` запрещено |\n"
        "| `stage` | enum | нет | всегда `UNPROCESSED`; указать иную может только ADMIN |\n\n"
        "**Минимальный корректный запрос:** `{\"title\": \"...\", \"object_id\": \"OBJ-101\"}` — см. пример "
        "`create_minimal` в схеме тела.\n\n"
        "## Что приходит на выход\n\n"
        "**201** — карточка созданной заявки (`TicketDTO`, см. пример ниже). Ошибки в едином формате "
        "`{error, message, details, path, request_id}`:\n"
        "* **400** — бизнес-правила (недопустимая стадия, датa срока, состав участников);\n"
        "* **401** — нет/некорректный `X-API-Key` или `X-User-Id`;\n"
        "* **403** — модуль прав доступа запретил `ticket.create` (менеджер чужого объекта);\n"
        "* **404** — указанный сотрудник не найден в справочнике;\n"
        "* **422** — тело не прошло схему (например, `title` короче 3 символов);\n"
        "* **409/503** — целостность БД / хранилище недоступно.\n\n"
        "**Побочные эффекты:** запись в историю изменений (audit), уведомления исполнителям и "
        "наблюдателям, инвалидация кэша."
    ),
    responses={
        201: {
            "description": "Заявка создана — полная карточка (TicketDTO)",
            "content": {"application/json": {"example": TICKET_CARD_EXAMPLE}},
        },
        400: {
            "description": "Нарушение бизнес-правил (стадия, состав участников, согласование дат)",
            "content": {"application/json": {"example": _error_example(
                "validation_error",
                "Дата исполнения не может быть раньше даты постановки (due_date=2026-09-01, created_at=2026-09-29)",
                rule="due_date_vs_created_at",
            )}},
        },
        403: {
            "description": "Модуль прав доступа запретил создание заявки",
            "content": {"application/json": {"example": _error_example(
                "permission_denied",
                "Модуль прав доступа запретил действие 'ticket.create'",
                action="ticket.create", user_id="engineer_kuznetsov",
            )}},
        },
        404: {
            "description": "Указанный исполнитель/наблюдатель/постановщик не найден в справочнике",
            "content": {"application/json": {"example": _error_example(
                "not_found", "Сотрудник 'engineer_unknown' не найден в справочнике",
                user_id="engineer_unknown",
            )}},
        },
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
        "Результат кэшируется на короткий срок (CACHE_LIST_TTL).\n\n"
        "## Что подавать на вход (query-параметры, все опциональны)\n\n"
        "| Параметр | Тип | По умолчанию | Описание |\n"
        "|---|---|---|---|\n"
        "| `stage` | enum[] | — | фильтр по стадии; повторяемый: `?stage=UNPROCESSED&stage=IN_PROGRESS` |\n"
        "| `object_id` | string | — | заявки конкретного объекта, напр. `OBJ-101` |\n"
        "| `author_id` / `assignee_id` / `watcher_id` | string | — | user_id постановщика/исполнителя/наблюдателя |\n"
        "| `only_mine` | bool | `false` | только заявки, где являюсь участником |\n"
        "| `overdue_only` | bool | `false` | только просроченные (`due_date < сегодня`) |\n"
        "| `search` | string | — | подстрока по названию/описанию/объекту/источнику, 2–128 символов |\n"
        "| `created_from` / `created_to` | date-time | — | период постановки, ISO-8601: `2026-09-01T00:00:00Z` |\n"
        "| `order_by` | enum | `created_at` | `created_at` \\| `updated_at` \\| `due_date` \\| `title` \\| `stage` \\| `id` |\n"
        "| `order_desc` | bool | `true` | сортировка по убыванию |\n"
        "| `page` | int ≥ 1 | `1` | номер страницы |\n"
        "| `page_size` | int 1–200 | `20` | размер страницы |\n\n"
        "Пример: `GET /api/v1/tickets?stage=IN_PROGRESS&overdue_only=true&page=1&page_size=50`\n\n"
        "## Что приходит на выход\n\n"
        "**200** — `{total, page, page_size, pages, items[]}`, где `items` — карточки `TicketDTO` "
        "(пример ниже). Ошибки: **403** ticket.read запрещён, **422** недопустимое значение фильтра "
        "(например, неизвестная стадия или `page_size>200`)."
    ),
    responses={
        200: {
            "description": "Страница заявок",
            "content": {"application/json": {"example": TICKET_LIST_EXAMPLE}},
        },
        403: {
            "description": "Действие ticket.read запрещено модулем прав доступа",
            "content": {"application/json": {"example": _error_example(
                "permission_denied", "Модуль прав доступа запретил действие 'ticket.read'",
                action="ticket.read", user_id="observer_unknown",
            )}},
        },
    },
)
def list_tickets(
    request: Request,
    stage: Optional[List[TicketStage]] = Query(None, description="Фильтр по стадии (повторяемый параметр): UNPROCESSED | PENDING_MAINT | DIAGNOSTICS | IN_PROGRESS | CONTROL | PROCESSED"),
    object_id: Optional[str] = Query(None, max_length=64, description="Заявки конкретного объекта, напр. OBJ-101"),
    author_id: Optional[str] = Query(None, max_length=64, description="user_id постановщика"),
    assignee_id: Optional[str] = Query(None, max_length=64, description="user_id исполнителя"),
    watcher_id: Optional[str] = Query(None, max_length=64, description="user_id наблюдателя"),
    only_mine: bool = Query(False, description="Только заявки, где текущий пользователь — постановщик/исполнитель/наблюдатель"),
    overdue_only: bool = Query(False, description="Только просроченные активные заявки (due_date < сегодня)"),
    search: Optional[str] = Query(None, min_length=2, max_length=128, description="Подстрока по названию/описанию/объекту/источнику предупреждения (2–128 символов)"),
    created_from: Optional[datetime] = Query(None, description="Поставлены не раньше, ISO-8601 (2026-09-01T00:00:00Z)"),
    created_to: Optional[datetime] = Query(None, description="Поставлены не позже, ISO-8601"),
    order_by: str = Query("created_at", pattern="^(created_at|updated_at|due_date|title|stage|id)$", description="Поле сортировки"),
    order_desc: bool = Query(True, description="Сортировка по убыванию (по умолчанию — новые сверху)"),
    page: int = Query(1, ge=1, description="Номер страницы, начиная с 1"),
    page_size: int = Query(20, ge=1, le=200, description="Размер страницы (1–200)"),
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
def get_ticket(
    ticket_id: int,
    actor: ActorContext = Depends(get_actor),
    service: TicketService = Depends(get_ticket_service),
) -> TicketDTO:
    return service.get_ticket(ticket_id, actor)


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
