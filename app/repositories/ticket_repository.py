"""Слой доступа к данным заявок (PostgreSQL, SQLAlchemy Core - чистый SQL).

``BaseTicketRepository`` описывает контракт хранилища и общие утилиты сериализации;
``PostgresTicketRepository`` реализует его поверх PostgreSQL;
``InMemoryTicketRepository`` - тот же контракт в памяти процесса (тесты/демо без БД).

Репозиторий **не знает про права доступа** - он возвращает только те записи, о которых
его попросили с уже применёнными фильтрами видимости (см. ``TicketService``).
"""

from __future__ import annotations

import abc
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, asc, case, desc, func, insert, or_, select, text, update

from app.database import (
    db as default_db,
    metadata,
    ticket_history,
    ticket_notifications,
    ticket_users,
    tickets,
)
from app.models import ACTIVE_STAGES, TicketStage

#: поля, по которым разрешена сортировка списка заявок (защита от SQL-инъекций)
SORTABLE_FIELDS = {
    "created_at": tickets.c.created_at,
    "updated_at": tickets.c.updated_at,
    "due_date": tickets.c.due_date,
    "title": tickets.c.title,
    "stage": tickets.c.stage,
    "id": tickets.c.id,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dumps(value: Optional[Iterable[str]]) -> str:
    return json.dumps(list(value or []), ensure_ascii=False)


def _loads(value: Optional[str]) -> List[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


class BaseTicketRepository(abc.ABC):
    """Контракт хранилища заявок."""

    # ---------------------------------------------------------------- заявки
    @abc.abstractmethod
    def create(self, data: Dict[str, Any]) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def get_by_id(self, ticket_id: int) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def update(self, ticket_id: int, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]: ...

    @abc.abstractmethod
    def delete(self, ticket_id: int, hard: bool = False) -> bool: ...

    @abc.abstractmethod
    def search(self, filters: Dict[str, Any], order_by: str, order_desc: bool,
               offset: int, limit: int) -> Tuple[List[Dict[str, Any]], int]: ...

    @abc.abstractmethod
    def count_by_stage(self, object_ids: Optional[Sequence[str]], overdue_before: Optional[date],
                       participant_id: Optional[str]) -> Dict[str, int]: ...

    @abc.abstractmethod
    def object_summary(self, object_ids: Optional[Sequence[str]]) -> List[Dict[str, Any]]: ...

    # --------------------------------------------------------------- история
    @abc.abstractmethod
    def add_history(self, entry: Dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def get_history(self, ticket_id: int) -> List[Dict[str, Any]]: ...

    # --------------------------------------------------------- уведомления
    @abc.abstractmethod
    def add_notification(self, record: Dict[str, Any]) -> Dict[str, Any]: ...

    @abc.abstractmethod
    def list_notifications(
        self,
        recipient: Optional[str] = None,
        ticket_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]: ...

    # -------------------------------------------------- справочник людей
    @abc.abstractmethod
    def upsert_user(self, user: Dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def get_users(self, user_ids: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]: ...

    # ------------------------------------------------------------ утилиты
    @staticmethod
    def to_jsonable(row: Dict[str, Any]) -> Dict[str, Any]:
        """Привести строку БД к JSON-совместимому виду."""
        out = dict(row)
        out["assignee_ids"] = _loads(out.get("assignee_ids"))
        out["watcher_ids"] = _loads(out.get("watcher_ids"))
        if isinstance(out.get("stage"), TicketStage):
            out["stage"] = out["stage"].value
        return out


# =============================================================================
# PostgreSQL
# =============================================================================
class PostgresTicketRepository(BaseTicketRepository):
    """Хранилище заявок в PostgreSQL."""

    def __init__(self, database=None) -> None:
        self.db = database or default_db

    @property
    def engine(self):
        return self.db.engine

    # ---------------------------------------------------------------- create
    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(data)
        payload.setdefault("created_at", _now())
        payload.setdefault("updated_at", _now())
        payload["assignee_ids"] = _dumps(payload.get("assignee_ids"))
        payload["watcher_ids"] = _dumps(payload.get("watcher_ids"))
        stmt = (
            insert(tickets)
            .values(**payload)
            .returning(*(col for col in tickets.columns))
        )
        with self.db.connect() as conn:
            row = conn.execute(stmt).mappings().one()
            conn.commit()
        return self.to_jsonable(dict(row))

    # ------------------------------------------------------------------- get
    def get_by_id(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        stmt = select(tickets).where(and_(tickets.c.id == ticket_id, tickets.c.is_deleted == 0))
        with self.db.connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self.to_jsonable(dict(row)) if row else None

    # ---------------------------------------------------------------- update
    def update(self, ticket_id: int, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not changes:
            return self.get_by_id(ticket_id)
        payload = dict(changes)
        payload["updated_at"] = _now()
        if "assignee_ids" in payload:
            payload["assignee_ids"] = _dumps(payload["assignee_ids"])
        if "watcher_ids" in payload:
            payload["watcher_ids"] = _dumps(payload["watcher_ids"])
        if "stage" in payload:
            payload["stage"] = TicketStage(payload["stage"]).value
        stmt = (
            update(tickets)
            .where(and_(tickets.c.id == ticket_id, tickets.c.is_deleted == 0))
            .values(**payload)
            .returning(*tickets.columns)
        )
        with self.db.connect() as conn:
            row = conn.execute(stmt).mappings().first()
            conn.commit()
        return self.to_jsonable(dict(row)) if row else None

    # ---------------------------------------------------------------- delete
    def delete(self, ticket_id: int, hard: bool = False) -> bool:
        with self.db.connect() as conn:
            if hard:
                deleted = conn.execute(
                    text("DELETE FROM tickets WHERE id = :id"), {"id": ticket_id}
                ).rowcount
            else:
                deleted = conn.execute(
                    update(tickets)
                    .where(tickets.c.id == ticket_id)
                    .values(is_deleted=1, updated_at=_now())
                ).rowcount
            conn.commit()
        return bool(deleted)

    # ---------------------------------------------------------------- search
    def _filter_clauses(self, filters: Dict[str, Any]) -> List[Any]:
        clauses: List[Any] = [tickets.c.is_deleted == 0]
        if filters.get("object_ids") is not None:
            ids = list(filters["object_ids"])
            if not ids:
                clauses.append(text("false"))
            else:
                clauses.append(tickets.c.object_id.in_(ids))
        if filters.get("object_id"):
            clauses.append(tickets.c.object_id == filters["object_id"])
        if filters.get("stages"):
            clauses.append(tickets.c.stage.in_([TicketStage(s).value for s in filters["stages"]]))
        if filters.get("author_id"):
            clauses.append(tickets.c.author_id == filters["author_id"])
        if filters.get("assignee_id"):
            clauses.append(tickets.c.assignee_ids.like(f'%"{filters["assignee_id"]}"%'))
        if filters.get("watcher_id"):
            clauses.append(tickets.c.watcher_ids.like(f'%"{filters["watcher_id"]}"%'))
        if filters.get("participant_id"):
            pid = str(filters["participant_id"])
            clauses.append(
                or_(
                    tickets.c.author_id == pid,
                    tickets.c.assignee_ids.like(f'%"{pid}"%'),
                    tickets.c.watcher_ids.like(f'%"{pid}"%'),
                )
            )
        if filters.get("search"):
            needle = f"%{filters['search'].lower()}%"
            clauses.append(
                or_(
                    func.lower(tickets.c.title).like(needle),
                    func.lower(tickets.c.description).like(needle),
                    func.lower(tickets.c.object_id).like(needle),
                )
            )
        if filters.get("due_before"):
            clauses.append(
                or_(tickets.c.due_date.is_(None), tickets.c.due_date <= filters["due_before"])
            )
        if filters.get("overdue_before"):
            clauses.append(
                and_(
                    tickets.c.due_date.isnot(None),
                    tickets.c.due_date < filters["overdue_before"],
                    tickets.c.stage.notin_([TicketStage.PROCESSED.value]),
                )
            )
        if filters.get("created_from"):
            clauses.append(tickets.c.created_at >= filters["created_from"])
        if filters.get("created_to"):
            clauses.append(tickets.c.created_at <= filters["created_to"])
        return clauses

    def search(self, filters: Dict[str, Any], order_by: str = "created_at",
               order_desc: bool = True, offset: int = 0, limit: int = 20):
        clauses = self._filter_clauses(filters)
        order_col = SORTABLE_FIELDS.get(order_by, tickets.c.created_at)
        ordering = desc(order_col) if order_desc else asc(order_col)
        where = and_(*clauses)

        count_stmt = select(func.count()).select_from(tickets).where(where)
        data_stmt = select(tickets).where(where).order_by(ordering).offset(offset).limit(limit)

        with self.db.connect() as conn:
            total = conn.execute(count_stmt).scalar_one()
            rows = conn.execute(data_stmt).mappings().all()
        return [self.to_jsonable(dict(r)) for r in rows], int(total)

    # ------------------------------------------------------------------ stats
    def count_by_stage(self, object_ids: Optional[Sequence[str]], overdue_before: Optional[date],
                       participant_id: Optional[str]) -> Dict[str, int]:
        filters: Dict[str, Any] = {}
        if object_ids is not None:
            filters["object_ids"] = list(object_ids)
        if participant_id:
            filters["participant_id"] = participant_id
        clauses = self._filter_clauses(filters)

        stage_expr = case(
            *[(tickets.c.stage == s.value, s.value) for s in TicketStage],
            else_=None,
        )
        stmt = (
            select(stage_expr.label("stage"), func.count().label("cnt"))
            .where(and_(*clauses))
            .group_by(stage_expr)
        )
        result: Dict[str, int] = {s.value: 0 for s in TicketStage}
        with self.db.connect() as conn:
            for row in conn.execute(stmt).mappings():
                if row["stage"]:
                    result[row["stage"]] = int(row["cnt"])
            overdue = 0
            if overdue_before:
                overdue_clauses = self._filter_clauses({**filters, "overdue_before": overdue_before})
                overdue = int(
                    conn.execute(
                        select(func.count()).select_from(tickets).where(and_(*overdue_clauses))
                    ).scalar_one()
                )
        result["__overdue__"] = overdue
        return result

    def object_summary(self, object_ids: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
        filters: Dict[str, Any] = {}
        if object_ids is not None:
            filters["object_ids"] = list(object_ids)
        clauses = self._filter_clauses(filters)
        stmt = (
            select(
                tickets.c.object_id,
                func.count().label("total"),
                func.sum(case((tickets.c.stage != TicketStage.PROCESSED.value, 1), else_=0)).label("active"),
                func.sum(case((tickets.c.stage == TicketStage.PROCESSED.value, 1), else_=0)).label("processed"),
                func.max(tickets.c.created_at).label("last_ticket_at"),
            )
            .where(and_(*clauses))
            .group_by(tickets.c.object_id)
            .order_by(desc(func.max(tickets.c.created_at)))
        )
        with self.db.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            {
                "object_id": r["object_id"],
                "total": int(r["total"]),
                "active": int(r["active"] or 0),
                "processed": int(r["processed"] or 0),
                "last_ticket_at": r["last_ticket_at"],
            }
            for r in rows
        ]

    # ---------------------------------------------------------------- history
    def add_history(self, entry: Dict[str, Any]) -> None:
        payload = dict(entry)
        payload.setdefault("created_at", _now())
        with self.db.connect() as conn:
            conn.execute(insert(ticket_history).values(**payload))
            conn.commit()

    def get_history(self, ticket_id: int) -> List[Dict[str, Any]]:
        stmt = (
            select(ticket_history)
            .where(ticket_history.c.ticket_id == ticket_id)
            .order_by(asc(ticket_history.c.created_at), asc(ticket_history.c.id))
        )
        with self.db.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ notifications
    def add_notification(self, record: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(record)
        payload.setdefault("created_at", _now())
        payload.setdefault("channel", "inbox")
        payload.setdefault("status", "sent")
        stmt = (
            insert(ticket_notifications)
            .values(**payload)
            .returning(*(col for col in ticket_notifications.columns))
        )
        with self.db.connect() as conn:
            row = conn.execute(stmt).mappings().one()
            conn.commit()
        return dict(row)

    def list_notifications(self, recipient: Optional[str] = None, ticket_id: Optional[int] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        stmt = select(ticket_notifications)
        if recipient:
            stmt = stmt.where(ticket_notifications.c.recipient == recipient)
        if ticket_id:
            stmt = stmt.where(ticket_notifications.c.ticket_id == int(ticket_id))
        stmt = stmt.order_by(desc(ticket_notifications.c.created_at),
                             desc(ticket_notifications.c.id)).limit(int(limit))
        with self.db.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ users
    def upsert_user(self, user: Dict[str, Any]) -> None:
        values = {
            "user_id": user["user_id"],
            "full_name": user.get("full_name", user["user_id"]),
            "role": str(user.get("role", "OBSERVER")),
            "position": user.get("position"),
            "email": user.get("email"),
            "is_active": bool(user.get("is_active", True)),
        }
        conn_stmt = text(
            """
            INSERT INTO ticket_users (user_id, full_name, role, position, email, is_active)
            VALUES (:user_id, :full_name, CAST(:role AS user_role), :position, :email, :is_active)
            ON CONFLICT (user_id) DO UPDATE SET
                full_name = EXCLUDED.full_name,
                role      = EXCLUDED.role,
                position  = EXCLUDED.position,
                email     = EXCLUDED.email,
                is_active = EXCLUDED.is_active
            """
        )
        with self.db.connect() as conn:
            conn.execute(conn_stmt, values)
            conn.commit()

    def get_users(self, user_ids: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
        stmt = select(ticket_users)
        if user_ids:
            stmt = stmt.where(ticket_users.c.user_id.in_(list(set(user_ids))))
        with self.db.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return {
            r["user_id"]: {
                "user_id": r["user_id"],
                "full_name": r["full_name"],
                "role": r["role"],
                "position": r["position"],
                "email": r["email"],
                "is_active": bool(r["is_active"]),
            }
            for r in rows
        }

    # -------------------------------------------------------------- миграции
    def apply_schema(self) -> None:
        self.db.apply_schema()

    def seed_users(self, users: Iterable[Dict[str, Any]]) -> None:
        for user in users:
            self.upsert_user(user)

    def ping(self) -> bool:
        return self.db.ping()


# =============================================================================
# In-memory реализация (тесты / запуск без БД)
# =============================================================================
class InMemoryTicketRepository(BaseTicketRepository):
    """Тот же контракт, что и у PostgreSQL-репозитория, но данные в памяти процесса."""

    def __init__(self) -> None:
        self._tickets: Dict[int, Dict[str, Any]] = {}
        self._history: List[Dict[str, Any]] = []
        self._notifications: List[Dict[str, Any]] = []
        self._users: Dict[str, Dict[str, Any]] = {}
        self._ticket_seq = 0
        self._history_seq = 0
        self._notification_seq = 0

    # ---------------------------------------------------------------- create
    def create(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self._ticket_seq += 1
        now = _now()
        record = {
            "id": self._ticket_seq,
            "title": data["title"],
            "description": data.get("description", ""),
            "object_id": data["object_id"],
            "author_id": data["author_id"],
            "assignee_ids": list(data.get("assignee_ids") or []),
            "watcher_ids": list(data.get("watcher_ids") or []),
            "stage": TicketStage(data.get("stage", TicketStage.UNPROCESSED)).value,
            "due_date": data.get("due_date"),
            "created_at": data.get("created_at", now),
            "updated_at": now,
            "closed_at": data.get("closed_at"),
            "warning_source": data.get("warning_source"),
            "created_by": data.get("created_by", data["author_id"]),
            "is_deleted": 0,
        }
        self._tickets[record["id"]] = record
        return dict(record)

    def get_by_id(self, ticket_id: int) -> Optional[Dict[str, Any]]:
        record = self._tickets.get(int(ticket_id))
        if record is None or record["is_deleted"]:
            return None
        return dict(record)

    def update(self, ticket_id: int, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        record = self.get_by_id(ticket_id)
        if record is None:
            return None
        record.update(changes)
        if "stage" in changes:
            record["stage"] = TicketStage(changes["stage"]).value
        record["updated_at"] = _now()
        self._tickets[int(ticket_id)] = record
        return dict(record)

    def delete(self, ticket_id: int, hard: bool = False) -> bool:
        record = self._tickets.get(int(ticket_id))
        if record is None:
            return False
        if hard:
            self._tickets.pop(int(ticket_id), None)
        else:
            record["is_deleted"] = 1
            record["updated_at"] = _now()
        return True

    # ---------------------------------------------------------------- search
    @staticmethod
    def _match(record: Dict[str, Any], filters: Dict[str, Any]) -> bool:
        if record["is_deleted"]:
            return False
        if filters.get("object_ids") is not None and record["object_id"] not in set(filters["object_ids"]):
            return False
        if filters.get("object_id") and record["object_id"] != filters["object_id"]:
            return False
        if filters.get("stages") and record["stage"] not in {TicketStage(s).value for s in filters["stages"]}:
            return False
        if filters.get("author_id") and record["author_id"] != filters["author_id"]:
            return False
        if filters.get("assignee_id") and filters["assignee_id"] not in record["assignee_ids"]:
            return False
        if filters.get("watcher_id") and filters["watcher_id"] not in record["watcher_ids"]:
            return False
        pid = filters.get("participant_id")
        if pid and pid not in record["assignee_ids"] + record["watcher_ids"] + [record["author_id"]]:
            return False
        if filters.get("search"):
            needle = filters["search"].lower()
            blob = f"{record['title']} {record['description']} {record['object_id']}".lower()
            if needle not in blob:
                return False
        due = record.get("due_date")
        if filters.get("due_before") and due and due > filters["due_before"]:
            return False
        if filters.get("overdue_before"):
            if not due or due >= filters["overdue_before"] or record["stage"] == TicketStage.PROCESSED.value:
                return False
        if filters.get("created_from") and record["created_at"] < filters["created_from"]:
            return False
        if filters.get("created_to") and record["created_at"] > filters["created_to"]:
            return False
        return True

    def search(self, filters, order_by="created_at", order_desc=True, offset=0, limit=20):
        items = [dict(r) for r in self._tickets.values() if self._match(r, filters)]
        key = order_by if order_by in SORTABLE_FIELDS else "created_at"
        items.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse=order_desc)
        total = len(items)
        return items[offset: offset + limit], total

    def count_by_stage(self, object_ids, overdue_before, participant_id) -> Dict[str, int]:
        filters: Dict[str, Any] = {}
        if object_ids is not None:
            filters["object_ids"] = list(object_ids)
        if participant_id:
            filters["participant_id"] = participant_id
        result = {s.value: 0 for s in TicketStage}
        for record in self._tickets.values():
            if self._match(record, filters):
                result[record["stage"]] = result.get(record["stage"], 0) + 1
        result["__overdue__"] = (
            sum(
                1
                for r in self._tickets.values()
                if self._match(r, {**filters, "overdue_before": overdue_before})
            )
            if overdue_before
            else 0
        )
        return result

    def object_summary(self, object_ids) -> List[Dict[str, Any]]:
        filters: Dict[str, Any] = {}
        if object_ids is not None:
            filters["object_ids"] = list(object_ids)
        grouped: Dict[str, Dict[str, Any]] = {}
        for record in self._tickets.values():
            if not self._match(record, filters):
                continue
            bucket = grouped.setdefault(
                record["object_id"],
                {"object_id": record["object_id"], "total": 0, "active": 0, "processed": 0, "last_ticket_at": None},
            )
            bucket["total"] += 1
            if record["stage"] == TicketStage.PROCESSED.value:
                bucket["processed"] += 1
            else:
                bucket["active"] += 1
            if bucket["last_ticket_at"] is None or record["created_at"] > bucket["last_ticket_at"]:
                bucket["last_ticket_at"] = record["created_at"]
        return sorted(grouped.values(), key=lambda b: b["last_ticket_at"], reverse=True)

    # ---------------------------------------------------------------- history
    def add_history(self, entry: Dict[str, Any]) -> None:
        self._history_seq += 1
        record = {"id": self._history_seq, "created_at": _now(), **entry}
        self._history.append(record)

    def get_history(self, ticket_id: int) -> List[Dict[str, Any]]:
        return [dict(h) for h in self._history if h["ticket_id"] == int(ticket_id)]

    # ---------------------------------------------------------- notifications
    def add_notification(self, record: Dict[str, Any]) -> Dict[str, Any]:
        self._notification_seq += 1
        entry = {
            "id": self._notification_seq,
            "created_at": _now(),
            "channel": "inbox",
            "status": "sent",
            "error": None,
            **record,
        }
        self._notifications.append(entry)
        return dict(entry)

    def list_notifications(self, recipient: Optional[str] = None, ticket_id: Optional[int] = None,
                           limit: int = 50) -> List[Dict[str, Any]]:
        rows = [
            n for n in self._notifications
            if (recipient is None or n["recipient"] == recipient)
            and (ticket_id is None or n["ticket_id"] == int(ticket_id))
        ]
        rows.sort(key=lambda n: (-n["id"],))
        return [dict(n) for n in rows[: max(1, int(limit))]]

    # ------------------------------------------------------------------ users
    def upsert_user(self, user: Dict[str, Any]) -> None:
        self._users[user["user_id"]] = {
            "user_id": user["user_id"],
            "full_name": user.get("full_name", user["user_id"]),
            "role": user.get("role", "OBSERVER"),
            "position": user.get("position"),
            "email": user.get("email"),
            "is_active": bool(user.get("is_active", True)),
        }

    def get_users(self, user_ids=None) -> Dict[str, Dict[str, Any]]:
        if user_ids is None:
            return {k: dict(v) for k, v in self._users.items()}
        return {uid: dict(self._users[uid]) for uid in set(user_ids) if uid in self._users}

    def apply_schema(self) -> None:  # noqa: D401 - схема не нужна
        """В in-memory хранилище схема не требуется."""

    def seed_users(self, users: Iterable[Dict[str, Any]]) -> None:
        for user in users:
            self.upsert_user(user)

    def ping(self) -> bool:
        return True


def build_repository(database=None) -> BaseTicketRepository:
    """Фабрика хранилища: PostgreSQL (по умолчанию) или in-memory (url == 'memory')."""
    if database is not None and getattr(database, "url", "") == "memory":
        return InMemoryTicketRepository()
    url = getattr(database, "url", None) or default_db.url
    if url == "memory":
        return InMemoryTicketRepository()
    return PostgresTicketRepository(database or default_db)


__all__ = [
    "BaseTicketRepository",
    "PostgresTicketRepository",
    "InMemoryTicketRepository",
    "build_repository",
    "metadata",
    "ACTIVE_STAGES",
]
