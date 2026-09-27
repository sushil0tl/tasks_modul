"""Модель предметной области и схемы API (Pydantic v2).

Здесь описаны:
 * ``TicketStage`` - стадии жизненного цикла заявки + допустимые переходы;
 * ``Role`` / ``ActionType`` - роли и действия для взаимодействия с модулем прав доступа;
 * ``ActorContext`` - контекст вызова (кто и от имени чего действует);
 * Pydantic-схемы запросов/ответов REST API.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


# =============================================================================
# Стадии заявки
# =============================================================================
class TicketStage(str, enum.Enum):
    """Стадии: Не обработана -> Ожидает ТО -> Диагностика -> В работе -> Контроль -> Обработана."""

    UNPROCESSED = "UNPROCESSED"      # Не обработана
    PENDING_MAINT = "PENDING_MAINT"  # Ожидает ТО
    DIAGNOSTICS = "DIAGNOSTICS"      # Диагностика
    IN_PROGRESS = "IN_PROGRESS"      # В работе
    CONTROL = "CONTROL"              # Контроль
    PROCESSED = "PROCESSED"          # Обработана

    @property
    def title_ru(self) -> str:
        return STAGE_TITLES_RU[self]

    @property
    def next_stages(self) -> List["TicketStage"]:
        return STAGE_TRANSITIONS.get(self, [])

    @property
    def is_terminal(self) -> bool:
        return self is TicketStage.PROCESSED


STAGE_TITLES_RU: Dict[TicketStage, str] = {
    TicketStage.UNPROCESSED: "Не обработана",
    TicketStage.PENDING_MAINT: "Ожидает ТО",
    TicketStage.DIAGNOSTICS: "Диагностика",
    TicketStage.IN_PROGRESS: "В работе",
    TicketStage.CONTROL: "Контроль",
    TicketStage.PROCESSED: "Обработана",
}

#: маршрут движения заявки. Возврат на шаг назад разрешён (переделка/уточнение),
#: "прыжок" через стадию - только если включён settings.allow_stage_skip.
STAGE_TRANSITIONS: Dict[TicketStage, List[TicketStage]] = {
    TicketStage.UNPROCESSED: [TicketStage.PENDING_MAINT],
    TicketStage.PENDING_MAINT: [TicketStage.UNPROCESSED, TicketStage.DIAGNOSTICS],
    TicketStage.DIAGNOSTICS: [TicketStage.PENDING_MAINT, TicketStage.IN_PROGRESS],
    TicketStage.IN_PROGRESS: [TicketStage.DIAGNOSTICS, TicketStage.CONTROL],
    TicketStage.CONTROL: [TicketStage.IN_PROGRESS, TicketStage.PROCESSED],
    TicketStage.PROCESSED: [TicketStage.CONTROL],
}

#: стадии, в которых заявка считается активной (не закрытой)
ACTIVE_STAGES: List[TicketStage] = [
    TicketStage.UNPROCESSED,
    TicketStage.PENDING_MAINT,
    TicketStage.DIAGNOSTICS,
    TicketStage.IN_PROGRESS,
    TicketStage.CONTROL,
]


def can_transition(current: TicketStage, target: TicketStage, allow_skip: bool = False) -> bool:
    """Допустим ли переход ``current -> target``."""
    if current is target:
        return False
    if target in STAGE_TRANSITIONS.get(current, []):
        return True
    if allow_skip and target is not TicketStage.UNPROCESSED:
        # прямой выход в финал разрешён из любой активной стадии
        return current in ACTIVE_STAGES
    return False


# =============================================================================
# Роли и действия (контракт с модулем прав доступа)
# =============================================================================
class Role(str, enum.Enum):
    ADMIN = "ADMIN"
    MANAGER = "MANAGER"
    ENGINEER = "ENGINEER"
    OBSERVER = "OBSERVER"


class ActionType(str, enum.Enum):
    """Типы действий, по которым модуль прав доступа выдаёт разрешение."""

    TICKET_CREATE = "ticket.create"
    TICKET_READ = "ticket.read"
    TICKET_UPDATE = "ticket.update"
    TICKET_DELETE = "ticket.delete"
    TICKET_STAGE_CHANGE = "ticket.stage_change"
    TICKET_COMMENT = "ticket.comment"
    OBJECT_READ = "object.read"


#: какие поля заявки может менять каждая роль (используется как страховка,
#: основное решение принимает модуль прав доступа)
ROLE_EDITABLE_FIELDS: Dict[Role, set] = {
    Role.ADMIN: {
        "title", "description", "due_date", "author_id",
        "assignee_ids", "watcher_ids", "warning_source",
    },
    Role.MANAGER: {
        "title", "description", "due_date",
        "assignee_ids", "watcher_ids", "warning_source",
    },
    Role.ENGINEER: set(),
    Role.OBSERVER: set(),
}


# =============================================================================
# Контекст вызова
# =============================================================================
class ActorContext(BaseModel):
    """Кто выполняет действие (данные из заголовков + ответ модуля прав доступа)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_id: str
    full_name: str = ""
    role: Role = Role.OBSERVER
    #: полный набор ролей субъекта (обычно одна, но у администратора может быть совмещение)
    roles: List[Role] = Field(default_factory=list)
    #: доступные объекты: "*" - все, иначе список object_id
    object_scope: List[str] = Field(default_factory=lambda: ["*"])
    #: действия, разрешённые модулем прав доступа
    allowed_actions: List[str] = Field(default_factory=list)

    def has_role(self, role: Role) -> bool:
        return role in self.roles or role is self.role

    @property
    def sees_everything(self) -> bool:
        return Role.ADMIN in self.roles or self.role is Role.ADMIN

    @property
    def unrestricted_objects(self) -> bool:
        return "*" in self.object_scope

    def allows_object(self, object_id: str) -> bool:
        return self.unrestricted_objects or object_id in self.object_scope

    def allows_action(self, action: ActionType) -> bool:
        return "*" in self.allowed_actions or action.value in self.allowed_actions


# =============================================================================
# Схемы ответов
# =============================================================================
class StageInfo(BaseModel):
    code: TicketStage
    title: str
    next_stages: List[TicketStage] = Field(default_factory=list)


class PersonRef(BaseModel):
    """Участник заявки (постановщик / исполнитель / наблюдатель)."""

    user_id: str = Field(..., min_length=1, max_length=64)
    full_name: str = ""
    role: Optional[Role] = None

    @field_validator("user_id")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("user_id не может быть пустым")
        return value


#: максимальный горизонт планирования: дата исполнения не дальше N лет от сегодня
MAX_DUE_DATE_YEARS = 5


def _clean_text_list(value: Any, *, none_ok: bool = False) -> Any:
    """Общая нормализация списков user_id: строки -> strip -> уникальность."""
    if value is None:
        return None if none_ok else []
    if not isinstance(value, list):
        raise ValueError("ожидается список user_id")
    seen: List[str] = []
    for item in value:
        item = str(item).strip()
        if item and item not in seen:
            seen.append(item)
    return seen


class TicketBase(BaseModel):
    title: str = Field(..., min_length=3, max_length=255, examples=["Плановое ТО насоса ЦНС-180"])
    description: str = Field("", max_length=8000)
    object_id: str = Field(..., min_length=1, max_length=64, description="ID объекта, привязка обязательна")
    due_date: Optional[date] = Field(None, description="Дата исполнения (не в прошлом)")
    warning_source: Optional[str] = Field(None, max_length=128)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        """Обязательное поле: пробелы не считаются названием."""
        if not value or not value.strip():
            raise ValueError("Название заявки обязательно")
        return value.strip()

    @field_validator("object_id")
    @classmethod
    def _object_id_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("Привязка к объекту обязательна (object_id)")
        return value.strip()

    # Примечание: проверка due_date на «разумность» вынесена в model_validator
    # наследников, т.к. корректность срока зависит от даты постановки (created_at),
    # а для схем ответов (TicketDTO) входные проверки формы отключены.


class TicketCreateRequest(TicketBase):
    """Форма, которую заполняет менеджер после предупреждения с объекта."""

    author_id: Optional[str] = Field(
        None, max_length=64, description="Постановщик (по умолчанию - текущий пользователь)"
    )
    created_at: Optional[datetime] = Field(
        None,
        description="Дата постановки (по умолчанию - сейчас; можно указать факт времени предупреждения)",
    )
    assignee_ids: List[str] = Field(default_factory=list, description="Исполнители (*-много)")
    watcher_ids: List[str] = Field(default_factory=list, description="Наблюдатели (*)")
    stage: TicketStage = TicketStage.UNPROCESSED

    @field_validator("assignee_ids", "watcher_ids", mode="before")
    @classmethod
    def _uniq(cls, value: Any) -> Any:
        return _clean_text_list(value)

    @field_validator("author_id")
    @classmethod
    def _author(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("author_id не может быть пустой строкой")
        return value.strip() if value else None

    @field_validator("created_at")
    @classmethod
    def _created_at_not_future(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Дата постановки не может быть в будущем (заявку нельзя поставить «на потом»)."""
        if value is None:
            return value
        now = datetime.now(value.tzinfo) if value.tzinfo else datetime.now()
        if value > now + timedelta(minutes=5):
            raise ValueError("Дата постановки (created_at) не может быть в будущем")
        return value

    @model_validator(mode="after")
    def _dates_consistent(self) -> "TicketCreateRequest":
        """Корректность и согласование дат.

        * срок исполнения не дальше ``MAX_DUE_DATE_YEARS`` лет вперёд;
        * срок исполнения не раньше даты постановки (для заявок, создаваемых
          «задним числом» по давнему предупреждению, прошлый due_date допустим);
        * для заявки, создаваемой сегодня, дата исполнения не может быть в прошлом.
        """
        if self.due_date is not None:
            today = date.today()
            anchor = self.created_at.date() if self.created_at else today
            if anchor >= today and self.due_date < today:
                raise ValueError(
                    f"Дата исполнения не может быть в прошлом (сегодня {today.isoformat()})"
                )
            if self.due_date < anchor:
                raise ValueError(
                    "Дата исполнения не может быть раньше даты постановки "
                    f"(due_date={self.due_date}, created_at={anchor})"
                )
            if self.due_date > today + timedelta(days=365 * MAX_DUE_DATE_YEARS):
                raise ValueError(
                    f"Дата исполнения слишком далеко (максимум {MAX_DUE_DATE_YEARS} лет вперёд)"
                )
        overlap = set(self.assignee_ids) & set(self.watcher_ids)
        if overlap:
            raise ValueError(
                "Один и тот же сотрудник не может быть исполнителем и наблюдателем "
                f"одновременно: {sorted(overlap)}"
            )
        return self


class TicketUpdateRequest(BaseModel):
    """Частичное редактирование заявки (без стадий - для них отдельный эндпоинт)."""

    title: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = Field(None, max_length=8000)
    due_date: Optional[date] = None
    assignee_ids: Optional[List[str]] = None
    watcher_ids: Optional[List[str]] = None
    author_id: Optional[str] = Field(None, max_length=64)
    warning_source: Optional[str] = Field(None, max_length=128)
    comment: Optional[str] = Field(None, max_length=2000, description="Комментарий к правке (в историю)")

    @field_validator("assignee_ids", "watcher_ids", mode="before")
    @classmethod
    def _uniq(cls, value: Any) -> Any:
        return _clean_text_list(value, none_ok=True)

    @field_validator("title")
    @classmethod
    def _title(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("Название заявки не может быть пустым")
        return value.strip()

    @field_validator("due_date")
    @classmethod
    def _due(cls, value: Optional[date]) -> Optional[date]:
        if value is not None and value < date.today():
            raise ValueError(f"Дата исполнения не может быть в прошлом (сегодня {date.today().isoformat()})")
        return value


class StageChangeRequest(BaseModel):
    stage: TicketStage
    comment: Optional[str] = Field(None, max_length=2000)

    @field_validator("comment")
    @classmethod
    def _comment(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() if value and value.strip() else None


class CommentRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Текст комментария не может быть пустым")
        return value.strip()


class TicketDTO(TicketBase):
    """Заявка в том виде, в котором она отдаётся клиентам.

    Наследуется от ``TicketBase``, но схема ответа не должна повторно проходить
    «пользовательские» проверки формы (например, «дата исполнения не в прошлом»
    ломала бы выдачу просроченных заявок). Поэтому валидаторы входной формы
    здесь отключены.
    """

    model_config = ConfigDict(
        title={"validate_default": False},
        object_id={"validate_default": False},
        due_date={"validate_default": False},
    )

    id: int
    stage: TicketStage
    stage_title: str
    next_stages: List[TicketStage] = Field(default_factory=list)
    author: PersonRef
    assignees: List[PersonRef] = Field(default_factory=list)
    watchers: List[PersonRef] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
    created_by: str

    @property
    def is_active(self) -> bool:
        return self.stage is not TicketStage.PROCESSED


class TicketListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    items: List[TicketDTO]


class HistoryEntryDTO(BaseModel):
    id: int
    ticket_id: int
    action: str
    field: Optional[str] = None
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    comment: Optional[str] = None
    changed_by: PersonRef
    created_at: datetime


class StageCounterDTO(BaseModel):
    stage: TicketStage
    title: str
    count: int


class StageStatsResponse(BaseModel):
    total: int
    by_stage: List[StageCounterDTO]
    overdue: int = 0


class ObjectSummaryDTO(BaseModel):
    object_id: str
    total: int
    active: int
    processed: int
    last_ticket_at: Optional[datetime] = None


class UserDTO(BaseModel):
    user_id: str
    full_name: str
    role: Role
    position: Optional[str] = None
    email: Optional[EmailStr] = None
    is_active: bool = True


class UserInfoResponse(BaseModel):
    """Ответ /users/me - субъект глазами модуля прав доступа."""

    user: UserDTO
    roles: List[Role]
    object_scope: List[str]
    allowed_actions: List[str]


class NotificationDTO(BaseModel):
    """Уведомление, отправленное участнику заявки (запись журнала)."""

    id: Optional[int] = None
    ticket_id: int
    recipient: str
    recipient_name: str = ""
    event_type: str
    subject: str
    body: str
    channel: str = "inbox"
    status: str = "sent"
    error: Optional[str] = None
    created_at: Optional[datetime] = None


class CacheStatsResponse(BaseModel):
    """Состояние кэша (для эндпоинта /cache/stats)."""

    backend: str
    enabled: bool
    entries: int = 0
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    hit_rate: float = 0.0
    max_entries: Optional[int] = None
    extra: Dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    database: str
    access_control: str
    mode: str
    cache: str = "off"
    notifications: str = "off"


class MessageResponse(BaseModel):
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)
