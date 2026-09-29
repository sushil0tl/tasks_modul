"""Кеширование частых запросов микросервиса заявок.

Архитектура кэша (по принципу "best effort"):

* ``InMemoryCache``  - LRU + TTL в памяти процесса; работает всегда, не требует
  внешних сервисов. Используется как обязательный первый уровень.
* ``RedisCache``     - распределённый уровень (внешний Redis), включается при
  заданном ``REDIS_URL``. Ошибки Redis не ломают запрос: происходит прозрачный
  переход на in-process кэш.
* ``NoopCache``      - заглушка для ``CACHE_ENABLED=0`` и для тестов.

Публичный контракт (``CacheBackend``) одинаков для всех реализаций, поэтому
сервис не знает, где физически живёт кэш.

Инвалидация: все изменения заявки вызывают ``invalidate_ticket(ticket_id)``,
что удаляет карточку заявки, её историю и **все** списки/статистики (списки
зависят от фильтра и субъекта, дёшево пересчитать их надёжнее, чем размечать).
"""

from __future__ import annotations

import abc
import fnmatch
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def make_key(namespace: str, *parts: Any) -> str:
    """Стабильный ключ кэша: ``tickets:{namespace}:{sha1(частей...)}``.

    Части сериализуются в JSON, поэтому порядок и типы фильтров влияют на ключ,
    а ``None`` и отсутствие значения - нет.
    """
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]
    return f"{namespace}:{digest}"


class CacheBackend(abc.ABC):
    """Контракт кэша."""

    @abc.abstractmethod
    def get(self, key: str) -> Optional[Any]: ...

    @abc.abstractmethod
    def set(self, key: str, value: Any, ttl: int) -> None: ...

    @abc.abstractmethod
    def delete(self, key: str) -> None: ...

    @abc.abstractmethod
    def delete_pattern(self, pattern: str) -> int: ...

    @abc.abstractmethod
    def clear(self) -> None: ...

    @abc.abstractmethod
    def stats(self) -> Dict[str, Any]: ...

    # ------------------------------------------------------------- convenience
    def get_or_set(self, key: str, factory: Callable[[], Any], ttl: int) -> Tuple[Any, bool]:
        """Вернуть значение из кэша; при промахе вычислить ``factory()`` и запомнить.

        Возвращает ``(значение, was_cached)``.
        """
        cached = self.get(key)
        if cached is not None:
            return cached, True
        value = factory()
        if value is not None:
            self.set(key, value, ttl)
        return value, False


class NoopCache(CacheBackend):
    """Кэш-заглушка (``CACHE_ENABLED=0``)."""

    name = "noop"

    def get(self, key: str) -> Optional[Any]:
        return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        return None

    def delete(self, key: str) -> None:
        return None

    def delete_pattern(self, pattern: str) -> int:
        return 0

    def clear(self) -> None:
        return None

    def stats(self) -> Dict[str, Any]:
        return {"backend": self.name, "enabled": False}


class InMemoryCache(CacheBackend):
    """LRU-кэш с TTL в памяти процесса (потокобезопасный)."""

    name = "memory"

    def __init__(self, max_entries: int = 1024) -> None:
        self.max_entries = max(1, int(max_entries))
        self._data: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._lock = threading.RLock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    # ------------------------------------------------------------------ ops
    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                self.misses += 1
                return None
            expires_at, value = item
            if expires_at <= time.time():
                self._data.pop(key, None)
                self.misses += 1
                return None
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.time() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)
                self.evictions += 1

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def delete_pattern(self, pattern: str) -> int:
        removed = 0
        with self._lock:
            for key in [k for k in self._data if fnmatch.fnmatch(k, pattern)]:
                self._data.pop(key, None)
                removed += 1
        return removed

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._data.keys())

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.hits + self.misses
            return {
                "backend": self.name,
                "enabled": True,
                "entries": len(self._data),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }


class RedisCache(CacheBackend):
    """Распределённый кэш поверх Redis (значения хранятся как JSON).

    Любая ошибка сети/Redis логируется и превращается в промах кэша - кэш не
    должен влиять на доступность сервиса.
    """

    name = "redis"

    def __init__(self, url: str, prefix: str = "tickets", fallback: Optional[CacheBackend] = None,
                 timeout: float = 1.0) -> None:
        import redis  # локальный импорт: зависимость опциональна

        self.prefix = prefix
        self.fallback = fallback or InMemoryCache()
        self._client = redis.Redis.from_url(
            url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            decode_responses=True,
        )
        self._client.ping()  # проверяем доступность сразу
        logger.info("Redis cache connected: %s (prefix=%s)", url.split("@")[-1], prefix)

    # ------------------------------------------------------------------ ops
    def _full(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def get(self, key: str) -> Optional[Any]:
        try:
            raw = self._client.get(self._full(key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis GET failed (%s) -> fallback to memory cache", exc)
            return self.fallback.get(key)
        if raw is None:
            return self.fallback.get(key)
        try:
            return json.loads(raw)
        except ValueError:  # pragma: no cover
            return None

    def set(self, key: str, value: Any, ttl: int) -> None:
        self.fallback.set(key, value, ttl)
        try:
            self._client.set(self._full(key), json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis SET failed (%s)", exc)

    def delete(self, key: str) -> None:
        self.fallback.delete(key)
        try:
            self._client.delete(self._full(key))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis DEL failed (%s)", exc)

    def delete_pattern(self, pattern: str) -> int:
        removed = self.fallback.delete_pattern(pattern)
        try:
            cursor = 0
            full = self._full(pattern)
            while True:
                cursor, keys = self._client.scan(cursor=cursor, match=full, count=200)
                if keys:
                    self._client.delete(*keys)
                    removed += len(keys)
                if cursor == 0:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis SCAN/DEL failed (%s)", exc)
        return removed

    def clear(self) -> None:
        self.fallback.clear()
        try:
            cursor = 0
            while True:
                cursor, keys = self._client.scan(cursor=cursor, match=f"{self.prefix}:*", count=500)
                if keys:
                    self._client.delete(*keys)
                if cursor == 0:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis clear failed (%s)", exc)

    def stats(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"backend": self.name, "enabled": True}
        try:
            raw = self._client.info(section="stats")
            info["redis_keyspace_hits"] = raw.get("keyspace_hits")
            info["redis_keyspace_misses"] = raw.get("keyspace_misses")
        except Exception as exc:  # noqa: BLE001
            info["redis_error"] = str(exc)
        info["local"] = self.fallback.stats()
        return info


class TicketCache:
    """Прикладная обёртка: знает пространства имён и правила инвалидации заявок."""

    NS_TICKET = "ticket"
    NS_LIST = "list"
    NS_STATS = "stats"
    NS_HISTORY = "history"
    NS_OBJECTS = "objects"
    NS_USERS = "users"

    def __init__(self, backend: CacheBackend, settings: Any) -> None:
        self.backend = backend
        self.settings = settings
        self.ticket_ttl = settings.cache_ticket_ttl
        self.list_ttl = settings.cache_list_ttl
        self.history_ttl = settings.cache_history_ttl

    # ------------------------------------------------------- пространства имён
    def ticket_key(self, ticket_id: int, actor_id: str) -> str:
        # ``*`` в префиксе гарантирует корректную инвалидацию по паттерну
        # ``ticket:{id}:*`` для любого субъекта.
        return make_key(f"{self.NS_TICKET}:{ticket_id}:*", actor_id)

    def list_key(self, filters: Dict[str, Any], pagination: Dict[str, Any]) -> str:
        return make_key(self.NS_LIST, filters, pagination)

    def history_key(self, ticket_id: int) -> str:
        return make_key(f"{self.NS_HISTORY}:{ticket_id}")

    def stats_key(self, scope: Optional[List[str]], object_id: Optional[str]) -> str:
        return make_key(self.NS_STATS, sorted(scope) if scope else "*", object_id)

    def objects_key(self, scope: Optional[List[str]]) -> str:
        return make_key(self.NS_OBJECTS, sorted(scope) if scope else "*")

    def users_key(self) -> str:
        return make_key(self.NS_USERS, "all")

    # ------------------------------------------------------------ операции
    def get(self, key: str) -> Optional[Any]:
        return self.backend.get(key)

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self.backend.set(key, value, ttl or self.list_ttl)

    def get_or_set(self, key: str, factory: Callable[[], Any], ttl: int) -> Tuple[Any, bool]:
        return self.backend.get_or_set(key, factory, ttl)

    # ---------------------------------------------------------- инвалидация
    def invalidate_ticket(self, ticket_id: int) -> None:
        """Вызывается после ЛЮБОГО изменения заявки.

        Удаляет карточку, историю и все списки/статистики (списочные ключи не
        размечены по ticket_id, поэтому сбрасываются целиком - это безопасно:
        стоимость пересчёта ниже стоимости рассинхронизации).
        """
        if isinstance(self.backend, NoopCache):
            return
        self.backend.delete_pattern(f"{self.NS_TICKET}:{ticket_id}:*")
        self.backend.delete(self.history_key(ticket_id))
        self.backend.delete_pattern(f"{self.NS_LIST}:*")
        self.backend.delete_pattern(f"{self.NS_STATS}:*")
        self.backend.delete_pattern(f"{self.NS_OBJECTS}:*")
        logger.debug("Cache invalidated for ticket %s", ticket_id)

    def invalidate_users(self) -> None:
        self.backend.delete(self.users_key())

    def clear(self) -> None:
        self.backend.clear()

    def stats(self) -> Dict[str, Any]:
        return self.backend.stats()


def build_cache(settings: Any) -> TicketCache:
    """Фабрика кэша: Redis (если задан и доступен) -> иначе in-memory LRU."""
    if not settings.cache_enabled:
        logger.warning("Кеширование отключено (CACHE_ENABLED=0)")
        return TicketCache(NoopCache(), settings)

    memory = InMemoryCache(max_entries=settings.cache_max_entries)
    if settings.redis_url:
        try:
            return TicketCache(
                RedisCache(settings.redis_url, prefix=settings.redis_cache_prefix, fallback=memory),
                settings,
            )
        except Exception as exc:  # noqa: BLE001 - Redis недоступен => деградируем молча
            logger.warning("Redis недоступен (%s) -> используется in-memory кэш", exc)
    return TicketCache(memory, settings)


__all__ = [
    "CacheBackend",
    "NoopCache",
    "InMemoryCache",
    "RedisCache",
    "TicketCache",
    "build_cache",
    "make_key",
]
