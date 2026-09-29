"""Бизнес-логика модуля заявок.

Один публичный метод = одна операция (создать / отредактировать / получить / список /
сменить стадию / удалить / комментарий / история / статистика).

Разделение ответственности:
 * права пользователей проверяет **модуль прав доступа** (``AccessControlClient``);
 * здесь остаются только продуктовые правила жизненного цикла заявки и
   "локальные" проверки участия (постановщик/исполнитель/наблюдатель),
   которые невозможно выразить без данных заявки;
 * SQL живёт в репозитории (:mod:`app.repositories.ticket_repository`).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.auth.access_control import AccessControlClient
from app.cache import TicketCache, build_cache
from app.config import get_settings
from app.exceptions import (
    ConflictError,
    IntegrityConflictError,
    NotFoundError,
    PermissionDeniedError,
    StageTransitionError,
    StorageError,
    TicketServiceError,
    ValidationError,
)
from app.logging_config import audit_event
from app.models import (
    ROLE_EDITABLE_FIELDS,
    STAGE_TRANSITIONS,
    ActionType,
    ActorContext,
    HistoryEntryDTO,
    ObjectSummaryDTO,
    PersonRef,
    Role,
    StageChangeRequest,
    StageCounterDTO,
    StageStatsResponse,
    TicketCreateRequest,
    TicketDTO,
    TicketListResponse,
    TicketStage,
    TicketUpdateRequest,
    UserDTO,
    UserInfoResponse,
    can_transition,
)
from app.repositories.ticket_repository import BaseTicketRepository
from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


class _NoopNotifications:
    """Заглушка, если уведомления отключены/не переданы (сервис остаётся рабочим)."""

    enabled = False

    def notify(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:  # noqa: D102
        return []

    def list_for_user(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return []

    def list_for_ticket(self, ticket_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        return []

#: действия, которые может выполнять участник заявки (кроме администратора)
PARTICIPANT_ACTIONS = {ActionType.TICKET_READ, ActionType.TICKET_STAGE_CHANGE, ActionType.TICKET_COMMENT}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except ValueError:
            return []
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return []


class TicketService:
    """Сервис управления заявками."""

    def __init__(self, repository: BaseTicketRepository, access: AccessControlClient,
                 cache: Optional[TicketCache] = None,
                 notifications: Optional[NotificationService] = None,
                 settings=None) -> None:
        self.repo = repository
        self.access = access
        self.settings = settings if settings is not None else get_settings()
        #: кэш частых запросов (LRU/Redis); по умолчанию - in-process LRU
        self.cache = cache if cache is not None else build_cache(self.settings)
        #: рассылка уведомлений исполнителям/наблюдателям
        self.notifications = notifications if notifications is not None else _NoopNotifications()

    # ======================================================================
    # 1. Создание заявки
    # ======================================================================
    def create_ticket(self, payload: TicketCreateRequest, actor: ActorContext) -> TicketDTO:
        """Создать заявку по предупреждению, пришедшему с объекта.

        Обязательна привязка к объекту (``object_id``) и право на создание.

        Двухуровневая валидация:
        * структурная - схема ``TicketCreateRequest`` (обязательные поля, длина,
          формат/корректность дат: срок не в прошлом, дата постановки не в будущем,
          due_date >= created_at, непересечение исполнителей и наблюдателей);
        * бизнес-правила - здесь (права, состав участников, стадия, неизвестные
          сотрудники).
        Ошибки БД превращаются в доменные исключения (см. :mod:`app.exceptions`).
        """
        object_id = (payload.object_id or "").strip()
        if not object_id:
            raise ValidationError("Заявка должна быть привязана к объекту (object_id обязателен)")

        self._require(ActionType.TICKET_CREATE, actor, object_id=object_id)

        author_id = (payload.author_id or actor.user_id).strip()
        assignees = self._normalize_people(payload.assignee_ids, "исполнители")
        watchers = self._normalize_people(payload.watcher_ids, "наблюдатели")
        stage = TicketStage(payload.stage)

        if stage is not TicketStage.UNPROCESSED and not actor.sees_everything:
            raise ValidationError(
                "Новая заявка создаётся на стадии 'Не обработана'; смену стадии выполните отдельно",
                stage=stage.value,
            )
        placed_at = payload.created_at or _now()
        if placed_at.tzinfo is None:
            placed_at = placed_at.replace(tzinfo=timezone.utc)
        # «сегодня» считаем в часовом поясе даты постановки (по умолчанию — UTC now):
        # иначе заявка, поставленная «задним числом», получает некоррежный якорь
        anchor_local = placed_at.astimezone(placed_at.tzinfo)
        if payload.due_date and payload.due_date < anchor_local.date():
            raise ValidationError(
                "Дата исполнения не может быть раньше даты постановки",
                due_date=str(payload.due_date),
                created_at=placed_at.isoformat(),
            )
        if payload.due_date and anchor_local.date() >= date.today() and payload.due_date < date.today():
            raise ValidationError(
                "Дата исполнения не может быть в прошлом",
                due_date=str(payload.due_date),
                today=date.today().isoformat(),
            )

        self._check_people_conflicts(author_id, assignees, watchers)

        # участники должны существовать в справочнике сотрудников
        for user_id in [author_id] + assignees + watchers:
            self._ensure_person(user_id)

        record = self._repo_call(
            self.repo.create,
            {
                "title": payload.title.strip(),
                "description": (payload.description or "").strip(),
                "object_id": object_id,
                "author_id": author_id,
                "assignee_ids": assignees,
                "watcher_ids": watchers,
                "stage": stage.value,
                "due_date": payload.due_date,
                "warning_source": payload.warning_source,
                "created_by": actor.user_id,
                "created_at": placed_at,
                "closed_at": _now() if stage is TicketStage.PROCESSED else None,
            },
            action="создание заявки",
        )

        self._log_history(
            record["id"],
            actor.user_id,
            action="CREATED",
            field=None,
            old_value=None,
            new_value=stage.value,
            comment=f"Заявка создана по предупреждению объекта {object_id}",
        )
        self.cache.invalidate_ticket(record["id"])
        # уведомляем исполнителей и наблюдателей о новой заявке
        self.notifications.notify("CREATED", record, actor.user_id)
        logger.info("Ticket %s created on object %s by %s", record["id"], object_id, actor.user_id)
        return self._to_dto(record)

    # ======================================================================
    # 2. Чтение
    # ======================================================================
    def get_ticket(self, ticket_id: int, actor: ActorContext) -> TicketDTO:
        """Вернуть карточку заявки (с проверкой права на чтение).

        Карточка кэшируется (``CACHE_TICKET_TTL``); инвалидация - при любом
        изменении заявки. Ключ содержит subject, чтобы не отдавать данные
        одной заявки тому, кому они не видны.
        """
        key = self.cache.ticket_key(int(ticket_id), actor.user_id)

        cached = self.cache.get(key)
        if cached is not None:
            logger.debug("Cache HIT ticket %s for %s", ticket_id, actor.user_id)
            return TicketDTO(**cached)

        record = self._get_raw(ticket_id)
        self._require_read(record, actor)
        dto = self._to_dto(record)
        self.cache.set(key, dto.model_dump(mode="json"), self.cache.ticket_ttl)
        logger.debug("Cache MISS ticket %s for %s", ticket_id, actor.user_id)
        return dto

    def list_tickets(
        self,
        actor: ActorContext,
        *,
        stages: Optional[Sequence[TicketStage]] = None,
        object_id: Optional[str] = None,
        author_id: Optional[str] = None,
        assignee_id: Optional[str] = None,
        watcher_id: Optional[str] = None,
        only_mine: bool = False,
        overdue_only: bool = False,
        search: Optional[str] = None,
        created_from: Optional[datetime] = None,
        created_to: Optional[datetime] = None,
        order_by: str = "created_at",
        order_desc: bool = True,
        page: int = 1,
        page_size: Optional[int] = None,
    ) -> TicketListResponse:
        """Список заявок с фильтрами и пагинацией, ограниченный видимостью субъекта."""
        page = max(int(page or 1), 1)
        default_size = self.settings.default_page_size
        page_size = min(max(int(page_size or default_size), 1), self.settings.max_page_size)

        filters: Dict[str, Any] = {}
        scope = self._visibility_scope(actor)
        if scope is not None:
            if object_id:
                if object_id not in scope:
                    raise PermissionDeniedError(
                        f"Объект '{object_id}' недоступен пользователю '{actor.user_id}'",
                        object_id=object_id,
                    )
                filters["object_ids"] = [object_id]
            else:
                filters["object_ids"] = list(scope)
        elif object_id:
            filters["object_id"] = object_id

        if stages:
            filters["stages"] = [TicketStage(s).value for s in stages]
        if author_id:
            filters["author_id"] = author_id
        if assignee_id:
            filters["assignee_id"] = assignee_id
        if watcher_id:
            filters["watcher_id"] = watcher_id
        if only_mine:
            filters["participant_id"] = actor.user_id
        if overdue_only:
            filters["overdue_before"] = date.today()
        if search:
            filters["search"] = search
        if created_from:
            filters["created_from"] = created_from
        if created_to:
            filters["created_to"] = created_to

        return self._list_tickets_cached(
            actor, filters,
            order_by=order_by, order_desc=order_desc, page=page, page_size=page_size,
        )

    def _list_tickets_cached(
        self, actor: ActorContext, filters: Dict[str, Any], *,
        order_by: str, order_desc: bool, page: int, page_size: int,
    ) -> TicketListResponse:
        """Список заявок через кэш (частые одинаковые запросы дёшевы для БД)."""
        pagination = {"order_by": order_by, "order_desc": order_desc, "page": page, "page_size": page_size}
        key = self.cache.list_key(filters, {**pagination, "actor": actor.user_id})
        cached = self.cache.get(key)
        if cached is not None:
            logger.debug("Cache HIT ticket list (%s)", key[:48])
            return TicketListResponse(**cached)

        records, total = self._repo_call(
            self.repo.search,
            filters,
            order_by=order_by,
            order_desc=order_desc,
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        items = [self._to_dto(r).model_dump(mode="json") for r in records]
        pages = (total + page_size - 1) // page_size if total else 0
        payload = {"total": total, "page": page, "page_size": page_size, "pages": pages, "items": items}
        self.cache.set(key, payload, self.cache.list_ttl)
        logger.debug("Cache MISS ticket list (%s)", key[:48])
        return TicketListResponse(**payload)

    def get_history(self, ticket_id: int, actor: ActorContext) -> List[HistoryEntryDTO]:
        """История изменений заявки (audit log; только для тех, кто видит заявку).

        Результат кэшируется на ``CACHE_HISTORY_TTL`` и сбрасывается при любом
        изменении заявки.
        """
        record = self._get_raw(ticket_id)
        self._require_read(record, actor)

        key = self.cache.history_key(int(ticket_id))
        cached = self.cache.get(key)
        if cached is not None:
            return [HistoryEntryDTO(**entry) for entry in cached]

        users = self.repo.get_users()
        entries = [self._to_history_dto(entry, users) for entry in self.repo.get_history(ticket_id)]
        self.cache.set(key, [e.model_dump(mode="json") for e in entries], self.cache.history_ttl)
        return entries

    # ======================================================================
    # 3. Редактирование
    # ======================================================================
    def update_ticket(
        self, ticket_id: int, payload: TicketUpdateRequest, actor: ActorContext
    ) -> TicketDTO:
        """Отредактировать заявку (стадия меняется отдельной операцией)."""
        record = self._get_raw(ticket_id)
        self._require_editable_fields(record, payload, actor)

        changes: Dict[str, Any] = {}
        if payload.title is not None:
            title = payload.title.strip()
            if len(title) < 3:
                raise ValidationError("Название заявки слишком короткое", title=title)
            changes["title"] = title
        if payload.description is not None:
            changes["description"] = payload.description.strip()
        if payload.due_date is not None:
            changes["due_date"] = payload.due_date
        if payload.warning_source is not None:
            changes["warning_source"] = payload.warning_source
        if payload.assignee_ids is not None:
            changes["assignee_ids"] = self._normalize_people(payload.assignee_ids, "исполнители")
        if payload.watcher_ids is not None:
            changes["watcher_ids"] = self._normalize_people(payload.watcher_ids, "наблюдатели")
        if payload.author_id is not None:
            changes["author_id"] = payload.author_id.strip()

        if not changes:
            raise ValidationError("Нечего обновлять: запрос не содержит изменяемых полей")

        # --- бизнес-валидация результата (с учётом уже сохранённых значений) ---
        final_author = changes.get("author_id") or record["author_id"]
        final_assignees = changes.get("assignee_ids", _as_list(record.get("assignee_ids")))
        final_watchers = changes.get("watcher_ids", _as_list(record.get("watcher_ids")))
        if not final_assignees:
            raise ValidationError(
                "У заявки должен остаться хотя бы один исполнитель", field="assignee_ids"
            )
        self._check_people_conflicts(final_author, final_assignees, final_watchers)

        final_due = changes.get("due_date", record.get("due_date"))
        placed_at = record.get("created_at")
        if final_due is not None and placed_at is not None:
            # якорь «сегодня» — в часовом поясе даты постановки (для naive считаем UTC)
            anchor = placed_at if placed_at.tzinfo else placed_at.replace(tzinfo=timezone.utc)
            anchor_local = anchor.astimezone(anchor.tzinfo)
            if final_due < anchor_local.date():
                raise ValidationError(
                    "Дата исполнения не может быть раньше даты постановки",
                    due_date=str(final_due),
                    created_at=anchor_local.date().isoformat(),
                )
            if anchor_local.date() >= date.today() and final_due < date.today():
                raise ValidationError(
                    "Дата исполнения не может быть в прошлом",
                    due_date=str(final_due),
                    today=date.today().isoformat(),
                )

        # исполнители обязаны быть известны системе (чтобы карточка показывала ФИО)
        for user_id in list(final_assignees) + list(final_watchers) + [final_author]:
            self._ensure_person(user_id)

        updated = self._repo_call(
            self.repo.update, ticket_id, changes, action=f"обновление заявки {ticket_id}"
        )
        if updated is None:
            raise NotFoundError(f"Заявка {ticket_id} не найдена", ticket_id=ticket_id)

        for field, new_value in changes.items():
            self._log_history(
                ticket_id,
                actor.user_id,
                action="UPDATED",
                field=field,
                old_value=self._stringify(record.get(field)),
                new_value=self._stringify(new_value),
                comment=payload.comment,
            )
        self.cache.invalidate_ticket(ticket_id)

        # уведомления: отдельно тем, кого добавили в исполнители/наблюдатели,
        # и общий - участникам об изменении заявки
        old_assignees = set(_as_list(record.get("assignee_ids")))
        new_assignees = set(changes.get("assignee_ids", []))
        added_assignees = [a for a in changes.get("assignee_ids", []) if a not in old_assignees]
        old_watchers = set(_as_list(record.get("watcher_ids")))
        added_watchers = [w for w in changes.get("watcher_ids", []) if w not in old_watchers]

        if added_assignees:
            self.notifications.notify(
                "ASSIGNEE_ADDED", updated, actor.user_id,
                added_assignees=added_assignees, comment=payload.comment,
            )
        if added_watchers:
            self.notifications.notify(
                "WATCHER_ADDED", updated, actor.user_id,
                added_watchers=added_watchers, comment=payload.comment,
            )
        general_fields = [f for f in changes if f not in ("assignee_ids", "watcher_ids")]
        if general_fields or (new_assignees != old_assignees) or payload.comment:
            self.notifications.notify(
                "UPDATED", updated, actor.user_id,
                changed_fields=[f for f in changes], comment=payload.comment,
            )

        logger.info("Ticket %s updated by %s (%s)", ticket_id, actor.user_id, ",".join(changes))
        return self._to_dto(updated)

    # ======================================================================
    # 4. Управление стадией
    # ======================================================================
    def change_stage(
        self, ticket_id: int, payload: StageChangeRequest, actor: ActorContext
    ) -> TicketDTO:
        """Перевести заявку на другую стадию (маршрут описан в ``STAGE_TRANSITIONS``)."""
        record = self._get_raw(ticket_id)
        current = TicketStage(record["stage"])
        target = TicketStage(payload.stage)

        self._require_stage_change(record, actor)

        if current is target:
            raise ValidationError(
                f"Заявка уже находится на стадии '{current.title_ru}'", stage=target.value
            )
        if not can_transition(current, target, allow_skip=self.settings.allow_stage_skip):
            raise StageTransitionError(
                f"Переход '{current.title_ru}' -> '{target.title_ru}' недопустим",
                current=current.value,
                target=target.value,
                allowed=[s.value for s in STAGE_TRANSITIONS.get(current, [])],
            )

        changes: Dict[str, Any] = {"stage": target.value}
        if target is TicketStage.PROCESSED:
            changes["closed_at"] = _now()
        elif record.get("closed_at"):
            changes["closed_at"] = None

        updated = self._repo_call(
            self.repo.update, ticket_id, changes, action=f"смена стадии заявки {ticket_id}"
        )
        assert updated is not None
        self._log_history(
            ticket_id,
            actor.user_id,
            action="STAGE_CHANGED",
            field="stage",
            old_value=current.value,
            new_value=target.value,
            comment=payload.comment or f"{current.title_ru} -> {target.title_ru}",
        )
        self.cache.invalidate_ticket(ticket_id)
        # исполнители и наблюдатели должны знать о движении заявки по маршруту
        self.notifications.notify(
            "STAGE_CHANGED", updated, actor.user_id,
            old_stage=current.value, new_stage=target.value, comment=payload.comment,
        )
        logger.info("Ticket %s stage %s -> %s by %s", ticket_id, current.value, target.value, actor.user_id)
        return self._to_dto(updated)

    # ======================================================================
    # 5. Удаление
    # ======================================================================
    def delete_ticket(self, ticket_id: int, actor: ActorContext, hard: bool = False) -> Dict[str, Any]:
        """Удалить заявку (по умолчанию - мягкое удаление)."""
        record = self._get_raw(ticket_id)
        self._require(ActionType.TICKET_DELETE, actor, object_id=record["object_id"], ticket_id=ticket_id)

        stage = TicketStage(record["stage"])
        if stage not in (TicketStage.UNPROCESSED, TicketStage.PROCESSED) and not actor.sees_everything:
            raise ConflictError(
                "Заявку в активной стадии может удалить только администратор "
                "(или переведите её в 'Обработана')",
                stage=stage.value,
            )

        removed = self.repo.delete(ticket_id, hard=hard)
        if not removed:
            raise NotFoundError(f"Заявка {ticket_id} не найдена", ticket_id=ticket_id)
        self._log_history(
            ticket_id,
            actor.user_id,
            action="DELETED",
            field="is_deleted",
            old_value="0",
            new_value="1" if not hard else "hard",
            comment="Удаление заявки",
        )
        self.cache.invalidate_ticket(ticket_id)
        self.notifications.notify("DELETED", record, actor.user_id)
        return {"ticket_id": ticket_id, "deleted": True, "mode": "hard" if hard else "soft"}

    # ======================================================================
    # 6. Комментарий
    # ======================================================================
    def add_comment(self, ticket_id: int, text_body: str, actor: ActorContext) -> HistoryEntryDTO:
        """Добавить комментарий к заявке (хранится в истории)."""
        record = self._get_raw(ticket_id)
        self._require(ActionType.TICKET_COMMENT, actor, object_id=record["object_id"], ticket_id=ticket_id)
        body = (text_body or "").strip()
        if not body:
            raise ValidationError("Текст комментария не может быть пустым")

        self._log_history(ticket_id, actor.user_id, action="COMMENTED", comment=body)
        self.cache.invalidate_ticket(ticket_id)
        self.notifications.notify("COMMENTED", record, actor.user_id, comment=body)
        entries = self.repo.get_history(ticket_id)
        users = self.repo.get_users()
        return self._to_history_dto(entries[-1], users)

    # ======================================================================
    # 7. Сводки
    # ======================================================================
    def stage_stats(self, actor: ActorContext, object_id: Optional[str] = None) -> StageStatsResponse:
        """Количество заявок по стадиям в границах видимости субъекта (кэшируется)."""
        scope = self._visibility_scope(actor)
        filters_scope: Optional[List[str]] = None
        if scope is not None:
            if object_id and object_id not in scope:
                raise PermissionDeniedError(
                    f"Объект '{object_id}' недоступен пользователю '{actor.user_id}'", object_id=object_id
                )
            filters_scope = [object_id] if object_id else list(scope)
        elif object_id:
            filters_scope = [object_id]

        key = self.cache.stats_key(filters_scope, object_id)
        cached = self.cache.get(key)
        if cached is not None:
            return StageStatsResponse(**cached)

        counts = self._repo_call(
            self.repo.count_by_stage, filters_scope, date.today(), participant_id=None,
            action="расчёт статистики по стадиям",
        )
        by_stage = [
            StageCounterDTO(stage=stage, title=stage.title_ru, count=counts.get(stage.value, 0))
            for stage in TicketStage
        ]
        total = sum(item.count for item in by_stage)
        response = StageStatsResponse(total=total, by_stage=by_stage, overdue=counts.get("__overdue__", 0))
        self.cache.set(key, response.model_dump(mode="json"), self.cache.list_ttl)
        return response

    def list_objects(self, actor: ActorContext) -> List[ObjectSummaryDTO]:
        """Объекты со сводкой по заявкам (для выбора объекта в форме; кэшируется)."""
        scope = self._visibility_scope(actor)
        key = self.cache.objects_key(None if scope is None else list(scope))
        cached = self.cache.get(key)
        if cached is not None:
            return [ObjectSummaryDTO(**row) for row in cached]
        rows = self._repo_call(
            self.repo.object_summary, None if scope is None else list(scope),
            action="сводка по объектам",
        )
        self.cache.set(key, [dict(r) for r in rows], self.cache.list_ttl)
        return [ObjectSummaryDTO(**row) for row in rows]

    def tickets_by_object(self, object_id: str, actor: ActorContext, **kwargs: Any) -> TicketListResponse:
        """Все заявки конкретного объекта."""
        return self.list_tickets(actor, object_id=object_id, **kwargs)

    def list_users(self, actor: ActorContext, role: Optional[Role] = None) -> List[UserDTO]:
        """Справочник сотрудников (чтобы выбрать исполнителя/наблюдателя)."""
        self._require(ActionType.TICKET_READ, actor)
        users = self.repo.get_users()
        result = [UserDTO(**user) for user in users.values()]
        if role is not None:
            result = [u for u in result if u.role is role]
        return sorted(result, key=lambda u: (u.role.value, u.full_name))

    def whoami(self, actor: ActorContext) -> UserInfoResponse:
        """Кто я глазами модуля прав доступа."""
        users = self.repo.get_users([actor.user_id])
        known = users.get(actor.user_id)
        user = UserDTO(
            user_id=actor.user_id,
            full_name=known["full_name"] if known else actor.full_name,
            role=actor.role,
            position=known.get("position") if known else None,
            email=(known.get("email") if known else None) or None,
            is_active=bool(known.get("is_active", True)) if known else True,
        )
        return UserInfoResponse(
            user=user,
            roles=list(actor.roles),
            object_scope=list(actor.object_scope),
            allowed_actions=list(actor.allowed_actions),
        )

    # ======================================================================
    # Вспомогательные методы (не часть публичного API)
    # ======================================================================
    def _repo_call(self, func, *args, action: str = "операция с БД", **kwargs):
        """Вызов репозитория с переводу системных ошибок -> доменные исключения.

        * IntegrityError (нарушение UNIQUE/FK/CHECK в PostgreSQL) - 409;
        * OperationalError / DBAPIError (БД недоступна, таблица отсутствует) - 503.
        Доменные TicketServiceError пробрасываются как есть.
        """
        try:
            return func(*args, **kwargs)
        except TicketServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - анализируем тип ошибки драйвера
            name = type(exc).__name__
            text = str(exc)
            lowered = text.lower()
            if "integrity" in name.lower() or "unique" in lowered or "foreign key" in lowered or "violates" in lowered:
                logger.warning("Integrity violation during %s: %s", action, text)
                raise IntegrityConflictError(
                    f"Нарушение целостности данных при '{action}': проверьте справочники и связи",
                    action=action,
                    reason=text.splitlines()[0][:300],
                ) from exc
            logger.exception("Storage failure during %s", action)
            raise StorageError(
                f"Хранилище недоступно при '{action}'; повторите запрос позже",
                action=action,
                reason=text.splitlines()[0][:300] if text else name,
            ) from exc

    def _get_raw(self, ticket_id: int) -> Dict[str, Any]:
        try:
            ticket_id = int(ticket_id)
        except (TypeError, ValueError):
            raise ValidationError("ID заявки должен быть целым числом", ticket_id=ticket_id)
        record = self._repo_call(self.repo.get_by_id, ticket_id, action=f"чтение заявки {ticket_id}")
        if record is None:
            raise NotFoundError(f"Заявка {ticket_id} не найдена", ticket_id=ticket_id)
        return record

    def _require(self, action: ActionType, actor: ActorContext, **kwargs: Any) -> None:
        """Обращение к модулю прав доступа."""
        self.access.require(actor.user_id, action, **kwargs)

    def _require_read(self, record: Dict[str, Any], actor: ActorContext) -> None:
        """Чтение: администратор видит всё; остальные - только заявки своих объектов."""
        object_id = record["object_id"]
        if actor.sees_everything:
            self._require(ActionType.TICKET_READ, actor, object_id=object_id, ticket_id=record["id"])
            return
        if not actor.allows_object(object_id):
            raise PermissionDeniedError(
                f"Заявка {record['id']} относится к объекту '{object_id}', "
                f"который вам не доступен",
                ticket_id=record["id"],
                object_id=object_id,
            )
        self._require(ActionType.TICKET_READ, actor, object_id=object_id, ticket_id=record["id"])

    def _require_stage_change(self, record: Dict[str, Any], actor: ActorContext) -> None:
        """Менять стадию могут: админ, менеджер, и исполнитель этой заявки."""
        object_id = record["object_id"]
        if actor.sees_everything or actor.has_role(Role.MANAGER) or self._is_executor(record, actor.user_id):
            # право действия и доступ к объекту подтверждает модуль прав доступа
            self._require(
                ActionType.TICKET_STAGE_CHANGE, actor, object_id=object_id, ticket_id=record["id"]
            )
            return
        raise PermissionDeniedError(
            "Смену стадии разрешают только администратор, менеджер или исполнитель заявки",
            ticket_id=record["id"],
            role=actor.role.value,
        )

    def _require_editable_fields(self, record: Dict[str, Any], payload: TicketUpdateRequest, actor: ActorContext) -> None:
        """Проверяем право на редактирование и состав полей для роли."""
        object_id = record["object_id"]
        requested = set(payload.model_dump(exclude_none=True, exclude={"comment"}).keys())
        if not requested:
            raise ValidationError("Нечего обновлять: пустой запрос")

        if actor.sees_everything:
            self._require(ActionType.TICKET_UPDATE, actor, object_id=object_id, ticket_id=record["id"])
            allowed = ROLE_EDITABLE_FIELDS[Role.ADMIN]
        elif (
            actor.has_role(Role.MANAGER)
            and self._is_author_or_watcher(record, actor.user_id)
            and actor.allows_object(object_id)
        ):
            self._require(ActionType.TICKET_UPDATE, actor, object_id=object_id, ticket_id=record["id"])
            allowed = ROLE_EDITABLE_FIELDS[Role.MANAGER]
        else:
            raise PermissionDeniedError(
                "Редактировать заявку могут администратор, а также менеджер - "
                "её постановщик или наблюдатель",
                ticket_id=record["id"],
                role=actor.role.value,
            )

        forbidden = requested - allowed
        if forbidden:
            raise PermissionDeniedError(
                f"Поле(я) {sorted(forbidden)} недоступны для изменения ролью '{actor.role.value}'",
                fields=sorted(forbidden),
            )

    @staticmethod
    def _is_participant(record: Dict[str, Any], user_id: str) -> bool:
        return (
            record.get("author_id") == user_id
            or user_id in _as_list(record.get("assignee_ids"))
            or user_id in _as_list(record.get("watcher_ids"))
        )

    @staticmethod
    def _is_executor(record: Dict[str, Any], user_id: str) -> bool:
        return user_id in _as_list(record.get("assignee_ids"))

    @staticmethod
    def _is_author_or_watcher(record: Dict[str, Any], user_id: str) -> bool:
        return record.get("author_id") == user_id or user_id in _as_list(record.get("watcher_ids"))

    def _visibility_scope(self, actor: ActorContext) -> Optional[List[str]]:
        """``None`` - видеть всё; иначе список разрешённых object_id."""
        if actor.sees_everything:
            return None
        try:
            objects = self.access.get_allowed_objects(actor.user_id)
        except Exception:  # noqa: BLE001 - если модуль не отдал объекты, опираемся на контекст
            objects = list(actor.object_scope)
        if "*" in objects:
            return None
        return list(objects)

    def _normalize_people(self, ids: Sequence[str], label: str) -> List[str]:
        result: List[str] = []
        for raw in ids or []:
            user_id = str(raw).strip()
            if not user_id:
                continue
            if user_id not in result:
                result.append(user_id)
        if label == "исполнители" and not result:
            raise ValidationError("У заявки должен быть хотя бы один исполнитель", field=label)
        return result

    @staticmethod
    def _check_people_conflicts(author_id: str, assignees: Sequence[str], watchers: Sequence[str]) -> None:
        """Бизнес-валидация состава участников (для создания и редактирования)."""
        overlap = set(assignees) & set(watchers)
        if overlap:
            raise ValidationError(
                "Один сотрудник не может быть исполнителем и наблюдателем одной заявки",
                users=sorted(overlap),
            )
        if author_id and author_id in set(assignees):
            raise ValidationError(
                "Постановщик не может быть исполнителем собственной заявки",
                author_id=author_id,
            )

    def _ensure_person(self, user_id: str) -> None:
        """Участник должен присутствовать в справочнике (подтягиваем из модуля прав).

        Индикатор «неизвестного пользователя»: модуль прав доступа для незнакомца
        отдаёт роль OBSERVER без каких-либо разрешений (см.
        ``InMemoryAccessControlClient.get_subject`` / HttpRemote - 403/PermissionDenied).
        """
        known = self.repo.get_users([user_id])
        if user_id in known:
            return
        try:
            subject = self.access.get_subject(user_id)
        except TicketServiceError as exc:
            raise ValidationError(
                f"Пользователь '{user_id}' не найден в справочнике сотрудников",
                user_id=user_id,
                reason=exc.message,
            ) from exc
        is_unknown = (
            not subject
            or (subject.get("role") == Role.OBSERVER.value
                and not subject.get("allowed_actions")
                and not subject.get("is_active", True))
        )
        if is_unknown:
            raise ValidationError(
                f"Пользователь '{user_id}' не найден в справочнике сотрудников", user_id=user_id
            )
        try:
            self.repo.upsert_user(subject)
        except Exception as exc:  # noqa: BLE001 - гонка при параллельном upsert допустима
            logger.warning("Failed to upsert user %s: %s", user_id, exc)

    def _log_history(self, ticket_id: int, changed_by: str, **kwargs: Any) -> None:
        entry = {"ticket_id": ticket_id, "changed_by": changed_by, **kwargs}
        try:
            self.repo.add_history(entry)
        except Exception:  # noqa: BLE001 - аудит не должен ломать основную операцию
            logger.exception("Failed to write history for ticket %s", ticket_id)
        # --- Audit Log (журнал изменений): БД-запись + структурированная
        # JSON-строка в logs/audit.log (для ELK/Loki и разбора инцидентов)
        audit_event(
            kwargs.get("action") or "CHANGE",
            ticket_id=ticket_id,
            actor=changed_by,
            field=kwargs.get("field"),
            old_value=kwargs.get("old_value"),
            new_value=kwargs.get("new_value"),
            comment=kwargs.get("comment"),
        )

    # ------------------------------------------------------------------ cache
    def cache_stats(self) -> Dict[str, Any]:
        """Состояние кэша (hit/miss, бэкенд) - для /cache/stats."""
        return self.cache.stats()

    def cache_clear(self) -> Dict[str, Any]:
        """Полный сброс кэша (административная операция)."""
        self.cache.clear()
        logger.info("Cache cleared by admin operation")
        return {"cleared": True, "backend": self.cache.backend.name}

    def list_notifications(self, actor: ActorContext, *, user_id: Optional[str] = None,
                           ticket_id: Optional[int] = None, mine: bool = False,
                           limit: int = 50) -> List[Dict[str, Any]]:
        """Журнал уведомлений.

        * ``mine=true`` - «ящик» текущего пользователя (X-User-Id);
        * ``user_id=...`` - ящик другого пользователя; доступно администратору
          и менеджеру (менеджер видит только уведомления по своим объектам);
        * ``ticket_id=...`` - уведомления по заявке; требует права чтения заявки.
        """
        if ticket_id is not None:
            record = self._get_raw(ticket_id)
            self._require_read(record, actor)
            return self.notifications.list_for_ticket(int(ticket_id), limit=limit)

        target = actor.user_id if mine or not user_id else str(user_id).strip()

        if target != actor.user_id:
            if not (actor.sees_everything or actor.has_role(Role.MANAGER)):
                raise PermissionDeniedError(
                    "Чужой список уведомлений доступен только администратору или менеджеру",
                    user_id=target,
                )
            rows = self.repo.list_notifications(recipient=target, limit=limit)
            if not actor.sees_everything:
                # менеджеру - только уведомления по заявкам его объектов
                allowed_objects = set(self._visibility_scope(actor) or [])
                visible_ids = set()
                for row in rows:
                    rec = self.repo.get_by_id(row["ticket_id"])
                    if rec and rec["object_id"] in allowed_objects:
                        visible_ids.add(row["ticket_id"])
                rows = [r for r in rows if r["ticket_id"] in visible_ids]
            return rows

        return self.notifications.list_for_user(target, limit=limit)

    @staticmethod
    def _stringify(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return json.dumps(list(value), ensure_ascii=False)
        if isinstance(value, (dict,)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _as_date(value: Any) -> Optional[date]:
        """Привести значение к ``date`` (datetime из БД/кэша может прийти datetime)."""
        if value is None or isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                return None
        return None

    # ------------------------------------------------------------------ DTO
    def _person(self, user_id: str, cache: Dict[str, Dict[str, Any]]) -> PersonRef:
        known = cache.get(user_id)
        if known is None:
            return PersonRef(user_id=user_id, full_name=user_id)
        role = known.get("role")
        return PersonRef(
            user_id=user_id,
            full_name=known.get("full_name") or user_id,
            role=Role(role) if role in {r.value for r in Role} else None,
        )

    def _to_dto(self, record: Dict[str, Any]) -> TicketDTO:
        people_ids = (
            [record["author_id"]]
            + _as_list(record.get("assignee_ids"))
            + _as_list(record.get("watcher_ids"))
            + [record.get("created_by")]
        )
        cache = self.repo.get_users([pid for pid in people_ids if pid])
        stage = TicketStage(record["stage"])
        return TicketDTO(
            id=record["id"],
            title=record["title"],
            description=record["description"],
            object_id=record["object_id"],
            due_date=self._as_date(record.get("due_date")),
            warning_source=record.get("warning_source"),
            stage=stage,
            stage_title=stage.title_ru,
            next_stages=STAGE_TRANSITIONS.get(stage, []),
            author=self._person(record["author_id"], cache),
            assignees=[self._person(uid, cache) for uid in _as_list(record.get("assignee_ids"))],
            watchers=[self._person(uid, cache) for uid in _as_list(record.get("watcher_ids"))],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
            closed_at=record.get("closed_at"),
            created_by=record.get("created_by") or record["author_id"],
        )

    def _to_history_dto(self, entry: Dict[str, Any], users: Dict[str, Dict[str, Any]]) -> HistoryEntryDTO:
        changed_by = entry["changed_by"]
        return HistoryEntryDTO(
            id=entry["id"],
            ticket_id=entry["ticket_id"],
            action=entry["action"],
            field=entry.get("field"),
            old_value=entry.get("old_value"),
            new_value=entry.get("new_value"),
            comment=entry.get("comment"),
            changed_by=self._person(changed_by, users),
            created_at=entry["created_at"],
        )
