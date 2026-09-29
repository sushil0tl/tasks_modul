"""Клиент **модуля прав доступа**.

Сервис заявок **не реализует RBAC сам**: он лишь обращается к отдельному микросервису
прав доступа и спрашивает: "разрешено ли субъекту X действие Y над заявкой Z?".

Контракт (описан в ``AccessControlClient``):
    GET  {base}/subjects/{user_id}                 -> профиль субъекта + его действия/объекты
    POST {base}/check                              -> {allowed: bool, reason: str}
    GET  {base}/objects/{user_id}                  -> список доступных object_id ("*" = все)

Две реализации:
    * :class:`HttpAccessControlClient`     - реальные HTTP-вызовы (продовая конфигурация);
    * :class:`InMemoryAccessControlClient` - встроенный stub для разработки/тестов,
      включается когда ``ACCESS_CONTROL_URL`` не задан.

Общая ошибка обращения к модулю => :class:`AccessControlUnavailableError`
(безопаснее отказать, чем разрешить).
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

import requests

from app.config import get_settings
from app.exceptions import AccessControlUnavailableError, PermissionDeniedError
from app.models import ActionType, ActorContext, Role

logger = logging.getLogger(__name__)


# =============================================================================
# Абстракция
# =============================================================================
class AccessControlClient(abc.ABC):
    """Интерфейс обращения к модулю прав доступа."""

    #: режим работы для /health
    mode: str = "abstract"

    @abc.abstractmethod
    def get_subject(self, user_id: str, hint_role: Optional[str] = None) -> Dict[str, Any]:
        """Профиль субъекта: user_id, full_name, role(s), allowed_actions, object_scope."""

    @abc.abstractmethod
    def check_permission(
        self,
        user_id: str,
        action: ActionType,
        *,
        object_id: Optional[str] = None,
        ticket_id: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Разрешено ли пользователю действие?"""

    @abc.abstractmethod
    def get_allowed_objects(self, user_id: str) -> Sequence[str]:
        """Объекты, данные которых пользователь имеет право видеть ("*" - все)."""

    # ------------------------- вспомогательное (общее для реализаций) --------
    def get_actor(self, user_id: str, hint_role: Optional[str] = None) -> ActorContext:
        """Собрать :class:`ActorContext` по данным модуля прав доступа.

        Роль из заголовка клиента используется только как подсказка: если модуль
        прав доступа знает пользователя лучше - доверяем модулю.
        """
        subject = self.get_subject(user_id, hint_role=hint_role)
        roles = [Role(r) for r in subject.get("roles", []) if _is_role(r)]
        primary = subject.get("role") or (roles[0].value if roles else Role.OBSERVER.value)
        return ActorContext(
            user_id=subject.get("user_id", user_id),
            full_name=subject.get("full_name", "") or user_id,
            role=Role(primary) if _is_role(primary) else Role.OBSERVER,
            roles=roles or [Role(primary)],
            object_scope=list(subject.get("object_scope") or ["*"]),
            allowed_actions=list(subject.get("allowed_actions") or []),
        )

    def require(self, user_id: str, action: ActionType, **kwargs: Any) -> bool:
        """Проверить разрешение; при отказе бросить :class:`PermissionDeniedError`."""
        if not self.check_permission(user_id, action, **kwargs):
            raise PermissionDeniedError(
                f"Модуль прав доступа запретил действие '{action.value}' "
                f"для пользователя '{user_id}'",
                action=action.value,
                user_id=user_id,
                **{k: v for k, v in (kwargs or {}).items() if v is not None},
            )
        return True

    def health(self) -> Dict[str, Any]:
        return {"mode": self.mode, "available": True}


def _is_role(value: Any) -> bool:
    try:
        Role(value)
        return True
    except ValueError:
        return False


# =============================================================================
# HTTP-реализация (прод)
# =============================================================================
class HttpAccessControlClient(AccessControlClient):
    """Тонкий HTTP-клиент внешнего модуля прав доступа."""

    mode = "http"

    def __init__(self, base_url: str, api_key: str, timeout: float = 3.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": api_key, "Content-Type": "application/json"})

    # ---------------------------------------------------------------- helpers
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            logger.error("Access control unavailable: %s %s (%s)", method, url, exc)
            raise AccessControlUnavailableError(
                "Модуль прав доступа недоступен, запрос отклонён", detail=str(exc)
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise AccessControlUnavailableError(
                "Модуль прав доступа вернул ошибку", status=resp.status_code, body=resp.text[:200]
            )
        return resp.json()

    # ------------------------------------------------------------ interface
    def get_subject(self, user_id: str, hint_role: Optional[str] = None) -> Dict[str, Any]:
        data = self._request("GET", f"/subjects/{user_id}")
        if data is None:
            raise PermissionDeniedError(f"Пользователь '{user_id}' неизвестен модулю прав доступа")
        return data

    def check_permission(
        self,
        user_id: str,
        action: ActionType,
        *,
        object_id: Optional[str] = None,
        ticket_id: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        payload = {
            "user_id": user_id,
            "action": action.value,
            "resource": "ticket",
            "object_id": object_id,
            "ticket_id": ticket_id,
            **(extra or {}),
        }
        data = self._request("POST", "/check", json=payload) or {}
        return bool(data.get("allowed", False))

    def get_allowed_objects(self, user_id: str) -> Sequence[str]:
        data = self._request("GET", f"/objects/{user_id}") or {}
        return list(data.get("objects", ["*"]))

    def health(self) -> Dict[str, Any]:
        try:
            self._request("GET", "/health")
            return {"mode": self.mode, "available": True, "url": self.base_url}
        except AccessControlUnavailableError as exc:
            return {"mode": self.mode, "available": False, "url": self.base_url, "error": exc.message}


# =============================================================================
# Встроенный stub (разработка/тесты)
# =============================================================================
#: матрица прав по умолчанию. Полные права на чтение есть только у ADMIN,
#: но "участие в заявке" (постановщик/исполнитель/наблюдатель) даёт право читать её
#: и менять стадию - см. ``policy.py`` внутри сервиса.
DEFAULT_ROLE_ACTIONS: Dict[Role, List[str]] = {
    Role.ADMIN: ["*"],
    Role.MANAGER: [
        ActionType.TICKET_CREATE.value,
        ActionType.TICKET_READ.value,
        ActionType.TICKET_UPDATE.value,
        ActionType.TICKET_STAGE_CHANGE.value,
        ActionType.TICKET_COMMENT.value,
        ActionType.OBJECT_READ.value,
    ],
    Role.ENGINEER: [
        ActionType.TICKET_READ.value,
        ActionType.TICKET_STAGE_CHANGE.value,
        ActionType.TICKET_COMMENT.value,
        ActionType.OBJECT_READ.value,
    ],
    Role.OBSERVER: [ActionType.TICKET_READ.value],
}

DEMO_USERS: List[Dict[str, Any]] = [
    {
        "user_id": "admin_sidorov",
        "full_name": "Сидоров Артём Павлович",
        "role": Role.ADMIN.value,
        "position": "Администратор системы",
        "email": "a.sidorov@example.com",
        "object_scope": ["*"],
    },
    {
        "user_id": "manager_ivanov",
        "full_name": "Иванов Иван Иванович",
        "role": Role.MANAGER.value,
        "position": "Ведущий менеджер",
        "email": "i.ivanov@example.com",
        "object_scope": ["*"],
    },
    {
        "user_id": "manager_petrova",
        "full_name": "Петрова Анна Сергеевна",
        "role": Role.MANAGER.value,
        "position": "Менеджер смены",
        "email": "a.petrova@example.com",
        "object_scope": ["*"],
    },
    {
        "user_id": "engineer_kuznetsov",
        "full_name": "Кузнецов Пётр Олегович",
        "role": Role.ENGINEER.value,
        "position": "Инженер по эксплуатации",
        "email": "p.kuznetsov@example.com",
        "object_scope": ["OBJ-101", "OBJ-102", "OBJ-105"],
    },
    {
        "user_id": "engineer_smirnov",
        "full_name": "Смирнов Дмитрий Александрович",
        "role": Role.ENGINEER.value,
        "position": "Инженер КИПиА",
        "email": "d.smirnov@example.com",
        "object_scope": ["OBJ-101", "OBJ-103"],
    },
]


class InMemoryAccessControlClient(AccessControlClient):
    """Локальная заглушка модуля прав доступа (хранит роли в памяти процесса)."""

    mode = "in-memory"

    def __init__(self, users: Optional[Iterable[Dict[str, Any]]] = None) -> None:
        self._users: Dict[str, Dict[str, Any]] = {}
        for user in users or DEMO_USERS:
            self.register_user(**user)

    # ------------------------------------------------------------- управление
    def register_user(
        self,
        user_id: str,
        full_name: str,
        role: str,
        position: str = "",
        email: Optional[str] = None,
        object_scope: Optional[List[str]] = None,
        allowed_actions: Optional[List[str]] = None,
        is_active: bool = True,
    ) -> Dict[str, Any]:
        role_enum = Role(role)
        record = {
            "user_id": user_id,
            "full_name": full_name,
            "role": role_enum.value,
            "roles": [role_enum.value],
            "position": position,
            "email": email,
            "object_scope": list(object_scope or ["*"]),
            "allowed_actions": list(
                allowed_actions
                if allowed_actions is not None
                else DEFAULT_ROLE_ACTIONS[role_enum]
            ),
            "is_active": is_active,
        }
        self._users[user_id] = record
        return record

    def unregister_user(self, user_id: str) -> None:
        self._users.pop(user_id, None)

    # ------------------------------------------------------------ interface
    def get_subject(self, user_id: str, hint_role: Optional[str] = None) -> Dict[str, Any]:
        record = self._users.get(user_id)
        if record is None:
            # неизвестный пользователь: считаем наблюдателем без доступа к объектам
            logger.warning("Unknown user '%s' requested - treated as OBSERVER", user_id)
            return {
                "user_id": user_id,
                "full_name": user_id,
                "role": Role.OBSERVER.value,
                "roles": [Role.OBSERVER.value],
                "object_scope": [],
                "allowed_actions": [],
                "is_active": False,
            }
        return dict(record)

    def check_permission(
        self,
        user_id: str,
        action: ActionType,
        *,
        object_id: Optional[str] = None,
        ticket_id: Optional[int] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        subject = self.get_subject(user_id)
        actions = subject.get("allowed_actions", [])
        if "*" not in actions and action.value not in actions:
            return False
        if object_id is not None:
            scope = subject.get("object_scope", [])
            if "*" not in scope and object_id not in scope:
                return False
        return True

    def get_allowed_objects(self, user_id: str) -> Sequence[str]:
        return list(self.get_subject(user_id).get("object_scope", []))

    def all_users(self) -> List[Dict[str, Any]]:
        return [dict(u) for u in self._users.values()]


# =============================================================================
# Фабрика
# =============================================================================
_client: Optional[AccessControlClient] = None


def get_access_control_client() -> AccessControlClient:
    """Единый клиент модуля прав доступа для процесса."""
    global _client
    if _client is None:
        settings = get_settings()
        if settings.access_control_url:
            logger.info("Access control: HTTP client -> %s", settings.access_control_url)
            _client = HttpAccessControlClient(
                settings.access_control_url,
                settings.access_control_api_key,
                settings.access_control_timeout,
            )
        else:
            logger.warning(
                "ACCESS_CONTROL_URL не задан - используется встроенный stub "
                "(только для разработки!)"
            )
            _client = InMemoryAccessControlClient()
    return _client


def set_access_control_client(client: Optional[AccessControlClient]) -> None:
    """Подмена клиента (тесты / интеграция)."""
    global _client
    _client = client
