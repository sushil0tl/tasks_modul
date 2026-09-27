"""Уведомления участникам заявки (исполнители, наблюдатели, постановщик).

Канал доставки выбирается по настройкам:

* ``WebhookNotifier``  - HTTP POST в ``NOTIFICATION_WEBHOOK_URL`` (почта/мессенджер
  инкапсулирует внешний сервис - это типично для микро-сервисной архитектуры);
* ``LoggingNotifier``  - консоль + файл логов (режим по умолчанию, без внешних
  зависимостей).

Независимо от канала каждое уведомление фиксируется в таблице
``ticket_notifications`` (журнал) и доступен через ``GET /notifications``.

Правила рассылки (метод ``notify``):
 * CREATED           -> исполнители + наблюдатели (постановщик не уведомляется);
 * STAGE_CHANGED     -> исполнители + наблюдатели (+ постановщик, если NOTIFY_AUTHOR);
 * UPDATED           -> исполнители + наблюдатели;
 * ASSIGNEE_ADDED    -> новые исполнители;
 * WATCHER_ADDED     -> новые наблюдатели;
 * COMMENTED         -> исполнители + наблюдатели + постановщик (кроме автора комм.);
 * DELETED           -> исполнители + наблюдатели.

Уведомления никогда не ломают основную операцию: все исключения перехватываются
и пишутся в журнал со статусом ``failed``.
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Dict, Iterable, List, Optional, Set

import requests

logger = logging.getLogger(__name__)


class NotificationChannel(abc.ABC):
    """Контракт канала доставки уведомлений."""

    name: str = "base"

    @abc.abstractmethod
    def send(self, message: Dict[str, Any]) -> None:
        """Отправить одно уведомление; при ошибке бросать исключение."""


class LoggingNotifier(NotificationChannel):
    """Канал по умолчанию: пишет уведомление в лог (stdout + файл логов)."""

    name = "log"

    def send(self, message: Dict[str, Any]) -> None:
        logger.info(
            "NOTIFY [%s] to=%s ticket=%s | %s | %s",
            message.get("event_type"),
            message.get("recipient"),
            message.get("ticket_id"),
            message.get("subject"),
            message.get("body"),
        )


class WebhookNotifier(NotificationChannel):
    """HTTP-канал: POST JSON во внешний сервис нотификаций."""

    name = "webhook"

    def __init__(self, url: str, timeout: float = 3.0, api_key: Optional[str] = None) -> None:
        self.url = url
        self.timeout = timeout
        self.api_key = api_key

    def send(self, message: Dict[str, Any]) -> None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        response = requests.post(self.url, json=message, headers=headers, timeout=self.timeout)
        response.raise_for_status()


class NotificationService:
    """Рассылка уведомлений по событиям жизненного цикла заявки."""

    def __init__(self, repository: Any, settings: Any,
                 channels: Optional[Iterable[NotificationChannel]] = None) -> None:
        self.repo = repository
        self.settings = settings
        self.enabled = bool(settings.notifications_enabled)
        self.notify_author = bool(getattr(settings, "notify_author", True))
        if channels is not None:
            self.channels: List[NotificationChannel] = list(channels)
        else:
            self.channels = [LoggingNotifier()]
            webhook = getattr(settings, "notification_webhook_url", "")
            if webhook and self.enabled:
                self.channels.append(
                    WebhookNotifier(webhook, timeout=settings.notification_timeout,
                                    api_key=getattr(settings, "access_control_api_key", ""))
                )

    # ------------------------------------------------------------------ API
    def notify(self, event_type: str, record: Dict[str, Any], actor: str,
               *, changed_fields: Optional[List[str]] = None, comment: Optional[str] = None,
               old_stage: Optional[str] = None, new_stage: Optional[str] = None,
               added_assignees: Optional[List[str]] = None,
               added_watchers: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Разослать уведомление всем заинтересованным участникам события.

        Возвращает список сохранённых записей журнала (для тестов/отладки).
        """
        if not self.enabled:
            return []
        try:
            recipients = self._recipients(
                event_type, record, actor,
                added_assignees=added_assignees, added_watchers=added_watchers,
            )
            if not recipients:
                return []
            subject, body = self._compose(
                event_type, record, actor,
                changed_fields=changed_fields, comment=comment,
                old_stage=old_stage, new_stage=new_stage,
                added_assignees=added_assignees, added_watchers=added_watchers,
            )
        except Exception:  # noqa: BLE001 - уведомления не должны ронять бизнес-операцию
            logger.exception("Failed to prepare notifications (%s)", event_type)
            return []

        saved: List[Dict[str, Any]] = []
        for recipient in recipients:
            saved.append(self._deliver(recipient, event_type, record, subject, body))
        return saved

    # ------------------------------------------------------ внутренняя логика
    def _recipients(self, event_type: str, record: Dict[str, Any], actor: str, *,
                    added_assignees: Optional[List[str]] = None,
                    added_watchers: Optional[List[str]] = None) -> Set[str]:
        assignees = set(self._people(record.get("assignee_ids")))
        watchers = set(self._people(record.get("watcher_ids")))
        author = record.get("author_id")

        if event_type == "ASSIGNEE_ADDED":
            targets = set(added_assignees or [])
        elif event_type == "WATCHER_ADDED":
            targets = set(added_watchers or [])
        elif event_type == "CREATED":
            targets = assignees | watchers
        elif event_type in ("COMMENTED", "STAGE_CHANGED", "UPDATED", "DELETED"):
            targets = assignees | watchers
            if self.notify_author and author:
                targets.add(author)
        else:
            targets = assignees | watchers

        # отправителю не пишем самому себе
        targets.discard(actor)
        targets.discard(None)
        return {t for t in targets if t}

    @staticmethod
    def _people(value: Any) -> List[str]:
        if isinstance(value, str):
            import json

            try:
                parsed = json.loads(value)
                return [str(v) for v in parsed] if isinstance(parsed, list) else []
            except ValueError:
                return []
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
        return []

    def _compose(self, event_type: str, record: Dict[str, Any], actor: str, *,
                 changed_fields: Optional[List[str]] = None, comment: Optional[str] = None,
                 old_stage: Optional[str] = None, new_stage: Optional[str] = None,
                 added_assignees: Optional[List[str]] = None,
                 added_watchers: Optional[List[str]] = None):
        names = self._names([actor])
        actor_name = names.get(actor, actor)
        ticket_no = record.get("id")
        title = record.get("title", "")
        object_id = record.get("object_id", "")

        subjects = {
            "CREATED": f"Новая заявка #{ticket_no}: {title}",
            "STAGE_CHANGED": f"Заявка #{ticket_no}: смена стадии",
            "UPDATED": f"Заявка #{ticket_no} изменена",
            "COMMENTED": f"Комментарий к заявке #{ticket_no}",
            "ASSIGNEE_ADDED": f"Вы назначены исполнителем по заявке #{ticket_no}",
            "WATCHER_ADDED": f"Вы добавлены наблюдателем по заявке #{ticket_no}",
            "DELETED": f"Заявка #{ticket_no} удалена",
        }
        subject = subjects.get(event_type, f"Событие по заявке #{ticket_no}")

        lines = [f"Объект: {object_id}", f"Постановщик/инициатор события: {actor_name}"]
        if event_type == "CREATED":
            lines.append(f"Описание: {(record.get('description') or '-')[:500]}")
            due = record.get("due_date")
            if due:
                lines.append(f"Срок исполнения: {due}")
            if record.get("warning_source"):
                lines.append(f"Источник предупреждения: {record['warning_source']}")
        elif event_type == "STAGE_CHANGED" and old_stage and new_stage:
            from app.models import TicketStage

            try:
                old_title = TicketStage(old_stage).title_ru
                new_title = TicketStage(new_stage).title_ru
            except ValueError:  # pragma: no cover
                old_title, new_title = old_stage, new_stage
            lines.append(f"Стадия: {old_title} -> {new_title}")
        elif event_type == "UPDATED" and changed_fields:
            lines.append("Изменённые поля: " + ", ".join(sorted(changed_fields)))
        elif event_type == "COMMENTED" and comment:
            lines.append(f"Текст: {comment[:500]}")
        elif event_type == "ASSIGNEE_ADDED" and added_assignees:
            lines.append("Назначены исполнители: " + ", ".join(added_assignees))
        elif event_type == "WATCHER_ADDED" and added_watchers:
            lines.append("Добавлены наблюдатели: " + ", ".join(added_watchers))

        if comment and event_type not in ("COMMENTED",):
            lines.append(f"Комментарий: {comment[:300]}")

        return subject, "\n".join(lines)

    def _names(self, user_ids: Iterable[str]) -> Dict[str, str]:
        ids = [u for u in user_ids if u]
        if not ids:
            return {}
        users = self.repo.get_users(ids)
        return {uid: users[uid].get("full_name") or uid for uid in users}

    def _deliver(self, recipient: str, event_type: str, record: Dict[str, Any],
                 subject: str, body: str) -> Dict[str, Any]:
        message = {
            "recipient": recipient,
            "event_type": event_type,
            "ticket_id": record.get("id"),
            "object_id": record.get("object_id"),
            "subject": subject[:255],
            "body": body,
        }
        status = "sent"
        error: Optional[str] = None
        channel_names = ",".join(ch.name for ch in self.channels) or "none"
        for channel in self.channels:
            try:
                channel.send(message)
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                error = f"{channel.name}: {exc}"
                logger.warning("Notification channel %s failed: %s", channel.name, exc)

        entry: Dict[str, Any] = {}
        try:
            entry = self.repo.add_notification(
                {
                    "ticket_id": record.get("id"),
                    "recipient": recipient,
                    "event_type": event_type,
                    "subject": subject[:255],
                    "body": body,
                    "channel": channel_names[:32],
                    "status": status,
                    "error": error,
                }
            )
        except Exception as exc:  # noqa: BLE001 - журнал не критичен
            logger.warning("Cannot persist notification journal: %s", exc)
            entry = {**message, "channel": channel_names, "status": status, "error": error}

        audit = logging.getLogger("app.audit")
        if getattr(audit, "handlers", None):
            audit.info(
                "audit NOTIFY",
                extra={
                    "event": "NOTIFY",
                    "ticket_id": record.get("id"),
                    "actor": message.get("recipient"),
                    "notification_event": event_type,
                    "status": status,
                },
            )
        return entry

    def list_for_user(self, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Последние уведомления пользователя (личный «ящик»)."""
        return self.repo.list_notifications(recipient=user_id, limit=limit)

    def list_for_ticket(self, ticket_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        """История уведомлений по заявке."""
        return self.repo.list_notifications(ticket_id=ticket_id, limit=limit)


__all__ = ["NotificationChannel", "LoggingNotifier", "WebhookNotifier", "NotificationService"]
