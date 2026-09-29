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
    """Элемент справочника стадий (ответ GET /tickets/stages)."""

    code: TicketStage = Field(..., description="Латинский код стадии (используется в API)")
    title: str = Field(..., description="Русское название стадии")
    next_stages: List[TicketStage] = Field(
        default_factory=list,
        description=(
            "Куда можно перейти из этой стадии. Пустой список у финальной стадии; "
            "фактический переход проверяется сервисом (PUT /tickets/{id}/stage)."
        ),
    )


class PersonRef(BaseModel):
    """Участник заявки (постановщик / исполнитель / наблюдатель) — только для ответов API."""

    user_id: str = Field(
        ..., min_length=1, max_length=64,
        description="Уникальный ID сотрудника (совпадает с X-User-Id и значениями в assignee_ids/watcher_ids)",
        examples=["engineer_kuznetsov"],
    )
    full_name: str = Field("", description="ФИО сотрудника из справочника модуля прав доступа", examples=["Кузнецов Пётр Олегович"])
    role: Optional[Role] = Field(None, description="Роль сотрудника: ADMIN | MANAGER | ENGINEER | OBSERVER")

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

def _schema_examples(*pairs):
    """Собирает ``examples`` для JSON-схемы Pydantic v2.11 (без json_schema_example)."""
    return [{name: {"summary": summary, "value": value}} for name, summary, value in pairs]



class TicketBase(BaseModel):
    """Поля заявки, общие для формы создания и карточки в ответах API."""

    title: str = Field(
        ...,
        min_length=3,
        max_length=255,
        description="**Обязательно.** Название заявки (3–255 символов, пробелы не считаются названием)",
        examples=["Плановое ТО насоса ЦНС-180"],
    )
    description: str = Field(
        "",
        max_length=8000,
        description="Описание проблемы/работ. Непустой текст настоятельно рекомендуется",
        examples=["По предупреждению SCADA: вибрация подшипника 7.2 мм/с, рост температуры корпуса"],
    )
    object_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "**Обязательно.** ID объекта-источника предупреждения — привязка заявки к объекту. "
            "Список доступных объектов: `GET /api/v1/objects/allowed`"
        ),
        examples=["OBJ-101"],
    )
    due_date: Optional[date] = Field(
        None,
        description=(
            "Дата исполнения (срок), формат `ГГГГ-ММ-ДД`. Правила: не раньше даты постановки; "
            "для заявок «сегодня» — не в прошлом; горизонт планирования — максимум 5 лет вперёд"
        ),
        json_schema_extra={"format": "date", "examples": ["2026-10-15"]},
    )
    warning_source: Optional[str] = Field(
        None,
        max_length=128,
        description="Источник/код предупреждения (идентификатор события из системы мониторинга), если заявка создана по сигналу",
        examples=["SCADA/WARN-2026-09-29-014"],
    )

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
    """**Тело запроса `POST /api/v1/tickets`** — форма, которую заполняет менеджер после
    предупреждения с объекта.

    Обязательные поля: `title`, `object_id`. Остальные — опциональны; если `author_id`
    не указан, постановщиком становится текущий пользователь (заголовок `X-User-Id`).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": _schema_examples(
                (
                    "create_minimal",
                    "Минимальный запрос: только обязательные поля (title + object_id)",
                    {"title": "Заявка по предупреждению датчика давления", "object_id": "OBJ-102"},
                ),
                (
                    "create_full",
                    "Полная форма менеджера после предупреждения с объекта",
                    {
                        "title": "Плановое ТО насоса ЦНС-180",
                        "description": "По предупреждению SCADA: вибрация подшипника 7.2 мм/с, рост температуры корпуса",
                        "object_id": "OBJ-101",
                        "due_date": "2026-10-15",
                        "warning_source": "SCADA/WARN-2026-09-29-014",
                        "author_id": "manager_ivanov",
                        "created_at": "2026-09-29T09:57:31Z",
                        "assignee_ids": ["engineer_kuznetsov", "engineer_smirnov"],
                        "watcher_ids": ["manager_petrova"],
                        "stage": "UNPROCESSED",
                    },
                ),
            )
        }
    )

    author_id: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "Постановщик заявки (user_id). **По умолчанию — текущий пользователь** (X-User-Id). "
            "Постановщик не может быть одновременно исполнителем этой же заявки. "
            "Список сотрудников: `GET /api/v1/users`"
        ),
        examples=["manager_ivanov"],
    )
    created_at: Optional[datetime] = Field(
        None,
        description=(
            "Дата постановки заявки, ISO-8601 (`2026-09-29T10:00:00Z`). "
            "**По умолчанию — текущее время.** Нельзя указать будущее (допуск +5 минут на часы клиента); "
            "можно «задним числом» — по фактическому времени предупреждения"
        ),
        json_schema_extra={"format": "date-time", "examples": ["2026-09-29T09:57:31Z"]},
    )
    assignee_ids: List[str] = Field(
        default_factory=list,
        description=(
            "**Исполнители (может быть несколько).** Список user_id из справочника `GET /api/v1/users`. "
            "Дубликаты и пустые строки отбрасываются; один сотрудник не может быть одновременно "
            "исполнителем и наблюдателем; при изменении состава новые исполнители получают уведомление"
        ),
        json_schema_extra={"examples": [["engineer_kuznetsov", "engineer_smirnov"]]},
    )
    watcher_ids: List[str] = Field(
        default_factory=list,
        description=(
            "**Наблюдатели (может быть несколько).** Список user_id; получают уведомления по заявке, "
            "но не влияют на её выполнение"
        ),
        json_schema_extra={"examples": [["manager_petrova"]]},
    )
    stage: TicketStage = Field(
        TicketStage.UNPROCESSED,
        description=(
            "Стадия при создании. **Всегда `UNPROCESSED` («Не обработана»)** — исключение составляет "
            "ADMIN, который может создать заявку сразу в активной стадии. Смена стадии — отдельный "
            "эндпоинт `PUT /tickets/{id}/stage`"
        ),
    )

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
    """**Тело запроса `PATCH /api/v1/tickets/{ticket_id}`** — частичное редактирование заявки.

    Все поля опциональны: меняется только то, что передано. Стадия здесь **не** меняется —
    для неё отдельный эндпоинт `PUT /tickets/{id}/stage`. Права: MANAGER (постановщик/наблюдатель)
    и ADMIN; ENGINEER поля не правит.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": _schema_examples(
                (
                    "update_dates",
                    "Перенести срок с пояснением (попадёт в историю изменений)",
                    {"due_date": "2026-10-20", "comment": "Срок увеличен из-за ожидания подшипника на складе"},
                ),
                (
                    "update_people",
                    "Сменить состав: исполнитель + наблюдатель (полная замена списков)",
                    {"assignee_ids": ["engineer_kuznetsov"], "watcher_ids": ["manager_petrova"]},
                ),
            )
        }
    )

    title: Optional[str] = Field(
        None, min_length=3, max_length=255,
        description="Новое название (3–255 символов). Пустая строка запрещена",
        examples=["ТО насоса ЦНС-180 — замена подшипника"],
    )
    description: Optional[str] = Field(
        None, max_length=8000,
        description="Новое описание проблемы/работ",
        examples=["Заменить подшипник качения, провести балансировку вала"],
    )
    due_date: Optional[date] = Field(
        None,
        description=(
            "Новый срок исполнения (`ГГГГ-ММ-ДД`). Не может быть в прошлом и раньше даты постановки; "
            "максимум 5 лет вперёд"
        ),
        json_schema_extra={"format": "date", "examples": ["2026-10-20"]},
    )
    assignee_ids: Optional[List[str]] = Field(
        None,
        description=(
            "**Полная замена** списка исполнителей (не дополнение). Новые исполнители получают "
            "уведомление `ASSIGNEE_ADDED`; итоговый состав должен содержать хотя бы одного исполнителя"
        ),
        json_schema_extra={"examples": [["engineer_kuznetsov"]]},
    )
    watcher_ids: Optional[List[str]] = Field(
        None,
        description="**Полная замена** списка наблюдателей; пересечение с исполнителями запрещено",
        json_schema_extra={"examples": [["manager_petrova", "admin_sidorov"]]},
    )
    author_id: Optional[str] = Field(
        None, max_length=64,
        description="Смена постановщика (доступно ADMIN/MANAGER)",
        examples=["manager_petrova"],
    )
    warning_source: Optional[str] = Field(
        None, max_length=128,
        description="Источник/код предупреждения",
        examples=["SCADA/WARN-2026-09-29-014"],
    )
    comment: Optional[str] = Field(
        None, max_length=2000,
        description="Пояснение к правке — попадёт в историю изменений (audit-log) как запись UPDATE",
        examples=["Срок увеличен из-за ожидания подшипника на складе"],
    )

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
    """**Тело запроса `PUT /api/v1/tickets/{ticket_id}/stage`** — смена стадии заявки.

    Допустим только переход по маршруту жизненного цикла (см. `GET /tickets/stages`):
    `UNPROCESSED -> PENDING_MAINT -> DIAGNOSTICS -> IN_PROGRESS -> CONTROL -> PROCESSED`.
    Возврат на шаг назад разрешён; «прыжок» через стадию — только при ALLOW_STAGE_SKIP.
    Право `ticket.stage_change` имеют MANAGER и ENGINEER (ADMIN — всё).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": _schema_examples(
                (
                    "next_stage",
                    "Перевести заявку «Не обработана» в «Ожидает ТО» с пояснением",
                    {"stage": "PENDING_MAINT", "comment": "Передано в плановое ТО, согласовано с диспетчером"},
                ),
            )
        }
    )

    stage: TicketStage = Field(
        ...,
        description=(
            "Целевая стадия: UNPROCESSED | PENDING_MAINT | DIAGNOSTICS | IN_PROGRESS | CONTROL | PROCESSED. "
            "Должна быть допустима из текущей, иначе 400"
        ),
        examples=["PENDING_MAINT"],
    )
    comment: Optional[str] = Field(
        None, max_length=2000,
        description="Пояснение смены стадии — попадёт в историю изменений (audit-log)",
        examples=["Передано в плановое ТО, согласовано с диспетчером"],
    )


class CommentRequest(BaseModel):
    """**Тело запроса `POST /api/v1/tickets/{ticket_id}/comments`** — комментарий к заявке."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": _schema_examples(
                ("comment", "Комментарий исполнителя по ходу работ", {"text": "Подшипник заказан, поставка ожидается 05.10"}),
            )
        }
    )

    text: str = Field(
        ..., min_length=1, max_length=4000,
        description="Текст комментария (не может быть пустым/только пробелы). Сохраняется в истории, участники получают уведомление COMMENTED",
        examples=["Подшипник заказан, поставка ожидается 05.10"],
    )

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Текст комментария не может быть пустым")
        return value.strip()


class TicketDTO(TicketBase):
    """**Ответы API с заявкой** (карточка `GET /tickets/{id}`, создание `POST /tickets`,
    редактирование `PATCH`, смена стадии `PUT .../stage`).

    Поля `title`, `description`, `object_id`, `due_date`, `warning_source` наследуются
    от ``TicketBase``. Схема ответа не проходит «пользовательские» проверки формы
    (выдача просроченных заявок не должна падать) — см. мягкие валидаторы ниже.
    """

    # Поля наследуются от TicketBase, но «пользовательские» проверки формы здесь
    # отключены (см. переопределения валидаторов ниже).
    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "examples": _schema_examples(
                (
                    "ticket_card",
                    "Карточка заявки (ответ POST/PATCH/PUT stage и GET /tickets/{id})",
                    {
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
                    },
                ),
            )
        },
    )

    id: int = Field(..., description="Уникальный номер заявки", examples=[1])
    stage: TicketStage = Field(..., description="Текущая стадия жизненного цикла")
    stage_title: str = Field(..., description="Русское название текущей стадии", examples=["Не обработана"])
    next_stages: List[TicketStage] = Field(
        default_factory=list,
        description="Стадии, в которые можно перевести заявку прямо сейчас (для кнопки «Далее» в UI)",
    )
    author: PersonRef = Field(..., description="Постановщик заявки")
    assignees: List[PersonRef] = Field(default_factory=list, description="Исполнители (может быть несколько)")
    watchers: List[PersonRef] = Field(default_factory=list, description="Наблюдатели (может быть несколько)")
    created_at: datetime = Field(..., description="Дата постановки заявки (ISO-8601)")
    updated_at: datetime = Field(..., description="Дата последнего изменения")
    closed_at: Optional[datetime] = Field(None, description="Дата закрытия (заполняется на стадии «Обработана»)")
    created_by: str = Field(..., description="user_id пользователя, создавшего запись", examples=["manager_ivanov"])

    # --- Отключение «пользовательских» проверок формы для схемы ответа -------
    # Схема ответа не должна падать на просроченных заявках, пробелах в данных
    # и т.п.; нормализация выполняется мягко, без raise.
    @field_validator("title", mode="before")
    @classmethod
    def _out_title(cls, value: Any) -> str:
        return (str(value).strip() if value else "") or "—"

    @field_validator("object_id", mode="before")
    @classmethod
    def _out_object_id(cls, value: Any) -> str:
        return str(value).strip() if value else "—"

    @field_validator("due_date", mode="before")
    @classmethod
    def _out_due_date(cls, value: Any) -> Any:
        """Допускаем строку 'YYYY-MM-DD' / datetime из хранилища без проверки дат."""
        if isinstance(value, datetime):
            return value.date()
        return value

    @property
    def is_active(self) -> bool:
        return self.stage is not TicketStage.PROCESSED


class TicketListResponse(BaseModel):
    """Постранический список заявок (ответ `GET /tickets`, `/tickets/by-object/{id}`, `/objects/{id}/tickets`)."""

    total: int = Field(..., description="Всего заявок, подходящих под фильтры (с учётом прав пользователя)", examples=[42])
    page: int = Field(..., description="Номер текущей страницы (начиная с 1)", examples=[1])
    page_size: int = Field(..., description="Размер страницы (1–200)", examples=[20])
    pages: int = Field(..., description="Общее число страниц", examples=[3])
    items: List[TicketDTO] = Field(default_factory=list, description="Заявки текущей страницы")


class HistoryEntryDTO(BaseModel):
    """Запись истории изменений заявки (audit-log), ответ `GET /tickets/{id}/history`."""

    id: int = Field(..., description="Идентификатор записи истории")
    ticket_id: int = Field(..., description="Номер заявки")
    action: str = Field(
        ...,
        description=(
            "Тип события: CREATED | UPDATED | STAGE_CHANGED | COMMENTED | DELETED | "
            "ASSIGNEE_ADDED | WATCHER_ADDED"
        ),
        examples=["STAGE_CHANGED"],
    )
    field: Optional[str] = Field(None, description="Изменённое поле (для UPDATED/STAGE_CHANGED)", examples=["stage"])
    old_value: Optional[str] = Field(None, description="Значение до изменения", examples=["UNPROCESSED"])
    new_value: Optional[str] = Field(None, description="Значение после изменения", examples=["PENDING_MAINT"])
    comment: Optional[str] = Field(None, description="Комментарий автора изменения (если передан)")
    changed_by: PersonRef = Field(..., description="Кто изменил")
    created_at: datetime = Field(..., description="Момент изменения (ISO-8601)")


class StageCounterDTO(BaseModel):
    stage: TicketStage = Field(..., description="Стадия")
    title: str = Field(..., description="Русское название стадии", examples=["В работе"])
    count: int = Field(..., description="Число заявок в этой стадии (в пределах видимости пользователя)")


class StageStatsResponse(BaseModel):
    """Сводка «сколько заявок на какой стадии» (`GET /tickets/stats/stages`)."""

    total: int = Field(..., description="Всего видимых заявок", examples=[42])
    by_stage: List[StageCounterDTO] = Field(default_factory=list, description="Счётчики по всем шести стадиям")
    overdue: int = Field(0, description="Просроченные активные заявки (due_date < сегодня)", examples=[3])


class ObjectSummaryDTO(BaseModel):
    """Сводка сервиса по объекту (`GET /objects`) — реестр объектов ведёт другой микросервис."""

    object_id: str = Field(..., description="ID объекта", examples=["OBJ-101"])
    total: int = Field(..., description="Всего заявок по объекту")
    active: int = Field(..., description="Активных (не «Обработана»)")
    processed: int = Field(..., description="Закрытых («Обработана»)")
    last_ticket_at: Optional[datetime] = Field(None, description="Дата последней заявки по объекту")


class UserDTO(BaseModel):
    """Сотрудник из справочника модуля прав доступа (`GET /users`) — кого можно назначить исполнителем/наблюдателем."""

    user_id: str = Field(..., description="Уникальный ID сотрудника (значение для assignee_ids/watcher_ids/author_id)", examples=["engineer_kuznetsov"])
    full_name: str = Field(..., description="ФИО", examples=["Кузнецов Пётр Олегович"])
    role: Role = Field(..., description="Роль: ADMIN | MANAGER | ENGINEER | OBSERVER")
    position: Optional[str] = Field(None, description="Должность", examples=["Инженер по ТО оборудования"])
    email: Optional[EmailStr] = Field(None, description="Электронная почта", examples=["kuznetsov@example.com"])
    is_active: bool = Field(True, description="Активен ли сотрудник (не уволен/заблокирован)")


class UserInfoResponse(BaseModel):
    """Ответ /users/me - субъект глазами модуля прав доступа."""

    user: UserDTO = Field(..., description="Карточка текущего пользователя (X-User-Id)")
    roles: List[Role] = Field(default_factory=list, description="Все роли субъекта (возможно совмещение)")
    object_scope: List[str] = Field(default_factory=list, description="Доступные объекты; `['*']` — все объекты", examples=[["OBJ-101", "OBJ-102"]])
    allowed_actions: List[str] = Field(
        default_factory=list,
        description="Разрешённые действия от модуля прав доступа",
        examples=[["ticket.create", "ticket.read", "ticket.update", "ticket.stage_change", "ticket.comment", "object.read"]],
    )


class NotificationDTO(BaseModel):
    """Уведомление, отправленное участнику заявки (запись журнала `ticket_notifications`;
    ответы `GET /notifications` и `GET /tickets/{id}/notifications`)."""

    id: Optional[int] = Field(None, description="Идентификатор записи журнала уведомлений")
    ticket_id: int = Field(..., description="Заявка, по которой отправлено уведомление", examples=[1])
    recipient: str = Field(..., description="user_id получателя", examples=["engineer_kuznetsov"])
    recipient_name: str = Field("", description="ФИО получателя", examples=["Кузнецов Пётр Олегович"])
    event_type: str = Field(
        ...,
        description="Событие: CREATED | UPDATED | STAGE_CHANGED | COMMENTED | DELETED | ASSIGNEE_ADDED | WATCHER_ADDED",
        examples=["CREATED"],
    )
    subject: str = Field(..., description="Тема уведомления", examples=["Новая заявка #1: Плановое ТО насоса ЦНС-180"])
    body: str = Field(..., description="Текст уведомления (что произошло, кто, срок)",
                      examples=["Менеджер Иванов И.И. поставил вам заявку №1 по объекту OBJ-101 со сроком 2026-10-15."])
    channel: str = Field("inbox", description="Канал доставки: inbox | webhook", examples=["inbox"])
    status: str = Field("sent", description="Статус доставки: sent | failed", examples=["sent"])
    error: Optional[str] = Field(None, description="Причина, если доставка не удалась")
    created_at: Optional[datetime] = Field(None, description="Момент формирования уведомления")


class CacheStatsResponse(BaseModel):
    """Состояние кэша (ответ `GET /cache/stats`)."""

    backend: str = Field(..., description="Тип бэкенда кэша", examples=["in-memory-lru"])
    enabled: bool = Field(..., description="Включён ли кэш (CACHE_ENABLED)")
    entries: int = Field(0, description="Текущее число записей в кэше")
    hits: int = Field(0, description="Попаданий в кэш с момента запуска")
    misses: int = Field(0, description="Промахов по кэшу")
    evictions: int = Field(0, description="Вытеснений записей (LRU/TTL)")
    hit_rate: float = Field(0.0, description="Доля попаданий, 0..1", examples=[0.72])
    max_entries: Optional[int] = Field(None, description="Максимальный размер кэша (CACHE_MAX_ENTRIES)")
    extra: Dict[str, Any] = Field(default_factory=dict, description="Дополнительные метрики бэкенда (например, Redis-инфо)")


class ErrorResponse(BaseModel):
    """**Единый формат ошибок всех эндпоинтов** (400/401/403/404/409/422/500/503)."""

    error: str = Field(
        ...,
        description=(
            "Машинный код ошибки: validation_error | unauthorized | permission_denied | not_found | "
            "integrity_conflict | storage_error | access_control_unavailable | internal_error"
        ),
        examples=["permission_denied"],
    )
    message: str = Field(..., description="Читаемое описание проблемы на русском языке",
                         examples=["Модуль прав доступа запретил действие 'ticket.update'"])
    details: Dict[str, Any] = Field(default_factory=dict, description="Контекст ошибки: action, user_id, stage, errors и т.п.")
    path: str = Field("", description="Путь запроса, на котором возникла ошибка", examples=["/api/v1/tickets/1"])
    request_id: Optional[str] = Field(None, description="Correlation id (X-Request-Id) для поиска в логах")


class HealthResponse(BaseModel):
    """Ответ `GET /health` — состояние сервиса и его зависимостей."""

    status: str = Field(..., description="ok | degraded", examples=["ok"])
    service: str = Field(..., description="Имя сервиса", examples=["Ticket Service"])
    version: str = Field(..., description="Версия API", examples=["1.0.0"])
    database: str = Field(..., description="Состояние хранилища: up | down", examples=["up"])
    access_control: str = Field(..., description="Состояние модуля прав доступа: up | down", examples=["up"])
    mode: str = Field(..., description="Режим работы: PostgresRepo/in-memory и т.п.", examples=["InMemoryTicketRepository/in-memory"])
    cache: str = Field("off", description="Бэкенд кэша или off", examples=["in-memory-lru"])
    notifications: str = Field("off", description="on | off — включены ли уведомления", examples=["on"])


class MessageResponse(BaseModel):
    """Общий ответ «действие выполнено» (удаление заявки, сброс кэша)."""

    message: str = Field(..., description="Результат операции человекомочитаемо", examples=["Заявка 1 удалена (soft removal)"])
    details: Dict[str, Any] = Field(default_factory=dict, description="Контекст: режим удаления, затронутые объекты")
