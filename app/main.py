"""Точка входа FastAPI-приложения микросервиса заявок.

Собирает слои: репозиторий (PostgreSQL) + клиент модуля прав доступа + бизнес-сервис,
регистрирует роутеры и обработчики ошибок.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.api.routes import cache as cache_routes
from app.api.routes import notifications as notification_routes
from app.api.routes import objects as objects_routes
from app.api.routes import tickets as tickets_routes
from app.api.routes import users as users_routes
from app.auth.access_control import (
    DEMO_USERS,
    InMemoryAccessControlClient,
    get_access_control_client,
)
from app.cache import build_cache
from app.config import Settings, get_settings
from app.exceptions import TicketServiceError
from app.logging_config import setup_logging
from app.models import HealthResponse
from app.repositories.ticket_repository import (
    BaseTicketRepository,
    InMemoryTicketRepository,
    PostgresTicketRepository,
)
from app.services.notification_service import NotificationService
from app.services.ticket_service import TicketService

setup_logging(get_settings())
logger = logging.getLogger("ticket_service")

# =============================================================================
# Метаданные Swagger / OpenAPI (полное описание для /docs и /openapi.json)
# =============================================================================
OPENAPI_DESCRIPTION = """\
## Микросервис управления заявками (Ticket Service)

Сервис ведёт **заявки** — классические задачи CRM: предупреждение приходит **с объекта**,
менеджер нажимает кнопку и заполняет форму, данные уходят в этот модуль, модуль создаёт задачу.

### Модель заявки
| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | integer | уникальный номер заявки |
| `title` | string | название (обязательно, 3–255 символов) |
| `description` | string | описание проблемы |
| `object_id` | string | **привязка к объекту-источнику предупреждения (обязательна)** |
| `author` | PersonRef | постановщик заявки |
| `assignees` | PersonRef[] | исполнители (может быть несколько) |
| `watchers` | PersonRef[] | наблюдатели (может быть несколько) |
| `stage` | TicketStage | текущая стадия жизненного цикла |
| `due_date` | date | дата (срок) исполнения |
| `created_at` | datetime | дата постановки заявки |
| `updated_at` | datetime | дата последнего изменения |
| `closed_at` | datetime | дата закрытия (стадия «Обработана») |
| `warning_source` | string | источник/код предупреждения |

### Жизненный цикл
```
UNPROCESSED (Не обработана) -> PENDING_MAINT (Ожидает ТО) -> DIAGNOSTICS (Диагностика)
-> IN_PROGRESS (В работе) -> CONTROL (Контроль) -> PROCESSED (Обработана)
```
Прямые «прыжки» через стадию запрещены (возврат на шаг назад разрешён).
Справочник переходов доступен в `GET /api/v1/tickets/stages`.

### Роли и доступы
Права проверяет внешний **модуль прав доступа** (`ACCESS_CONTROL_URL`). Сервис лишь
запрашивает разрешения по действиям `ticket.create/read/update/delete/stage_change/comment`,
`object.read`:
* **ADMIN** — видит все заявки всех объектов, может всё;
* **MANAGER** — создаёт и редактирует заявки, управляет стадией, обычно постановщик/наблюдатель;
* **ENGINEER** — управляет стадией назначенных ему заявок (исполнитель);
* **OBSERVER** — только чтение своих объектов.

### Аутентификация запросов (кнопка *Authorize*)
Все эндпоинты `/api/v1/*` требуют заголовки:
* `X-API-Key` — ключ вызывающей системы (межсервисная аутентификация, настраивается `TICKETS_API_KEY`);
* `X-User-Id` — ID пользователя, от имени которого выполняется запрос;
* `X-Role` — подсказка роли (`ADMIN` | `MANAGER` | `ENGINEER`), сверяется с модулем прав доступа.

Тестовые пользователи (демо-режим): `admin_sidorov`, `manager_ivanov`, `manager_petrova`,
`engineer_kuznetsov`, `engineer_smirnov`; объекты: `OBJ-101`, `OBJ-102`, `OBJ-103`, `OBJ-105`.

### Ключевые сценарии
1. **Создать заявку:** `POST /api/v1/tickets` (минимум `title` + `object_id`) — см. пример в описании операции.
2. **Сменить стадию:** `PUT /api/v1/tickets/{id}/stage` (только следующий шаг маршрута).
3. **Редактировать состав/сроки:** `PATCH /api/v1/tickets/{id}`.
4. **История изменений (audit):** `GET /api/v1/tickets/{id}/history`.
5. **Уведомления участника:** `GET /api/v1/notifications` (ящик текущего `X-User-Id`).
6. **Заявки объекта:** `GET /api/v1/tickets/by-object/{object_id}` или `GET /api/v1/objects/{object_id}/tickets`.

### Единый формат ошибок
```json
{
  "error": "permission_denied",
  "message": "Модуль прав доступа запретил действие 'ticket.update' ...",
  "details": {"action": "ticket.update", "user_id": "engineer_kuznetsov"},
  "path": "/api/v1/tickets/1",
  "request_id": "ab12cd34ef56"
}
```
| Код | Когда |
|-----|-------|
| 400 `validation_error` | бизнес-валидация (стадия, состав участников, даты) |
| 401 `unauthorized` | нет/некорректный `X-API-Key` или `X-User-Id` |
| 403 `permission_denied` | модуль прав доступа запретил действие |
| 404 `not_found` | заявка/пользователь не найдены |
| 409 `integrity_conflict` | нарушение ограничений целостности БД |
| 422 `validation_error` | Pydantic-валидация тела/параметров запроса |
| 503 `storage_error` / `access_control_unavailable` | недоступны PostgreSQL или модуль прав доступа |

### Кеширование
Карточки заявок, списки, статистика и история кэшируются (in-process LRU+TTL, опционально Redis).
Любое изменение заявки инвалидирует её кэш. Метрики — `GET /api/v1/cache/stats`, сброс — `POST /api/v1/cache/clear` (ADMIN).

### Уведомления
При создании/изменении/смене стадии/комментарии/удалении сервиса формирует уведомления
исполнителям, наблюдателям (и постановщику) и пишет их в журнал `ticket_notifications`.

---
Версия API: **v1**. Документация: Swagger UI — `/docs`, ReDoc — `/redoc`, схема — `/openapi.json`.
"""

OPENAPI_TAGS = [
    {
        "name": "Заявки",
        "description": (
            "Основной ресурс: создание, поиск/список, карточка, редактирование, "
            "смена стадии, комментарии, история изменений, удаление."
        ),
    },
    {
        "name": "Объекты",
        "description": (
            "Аналитика сервиса по объектам-источникам предупреждений (реестр объектов ведёт другой микросервис)."
        ),
    },
    {
        "name": "Пользователи",
        "description": "Справочник сотрудников (кого можно назначить исполнителем/наблюдателем) и текущий субъект.",
    },
    {
        "name": "Уведомления",
        "description": "Журнал уведомлений, автоматически рассылаемых участникам заявок.",
    },
    {
        "name": "Кеширование",
        "description": "Метрики кэша и его сброс (административный эндпоинт).",
    },
    {
        "name": "Сервис",
        "description": "Информация о сервисе и состояние зависимостей (healthcheck).",
    },
]




def build_repository(settings: Settings) -> BaseTicketRepository:
    """Хранилище: PostgreSQL; если БД недоступна и включён dev-режим - in-memory."""
    if settings.database_url == "memory":
        logger.warning("TICKETS_DATABASE_URL=memory -> in-memory хранилище (только для разработки)")
        return InMemoryTicketRepository()

    repository = PostgresTicketRepository()
    try:
        if settings.auto_create_schema:
            repository.apply_schema()
        repository.ping()
    except Exception as exc:  # noqa: BLE001
        logger.error("PostgreSQL недоступен (%s)", exc)
        raise
    return repository


def bootstrap(app: FastAPI, settings: Optional[Settings] = None) -> None:
    """Инициализация зависимостей приложения (БД, кэш, права доступа, уведомления, сервис)."""
    settings = settings or get_settings()
    setup_logging(settings)
    access = get_access_control_client()
    repository = build_repository(settings)
    cache = build_cache(settings)
    notifications = NotificationService(repository=repository, settings=settings)

    if settings.seed_demo_data and isinstance(access, InMemoryAccessControlClient):
        repository.seed_users(DEMO_USERS)
        logger.info("Seeded %d demo users into справочник", len(DEMO_USERS))

    app.state.settings = settings
    app.state.repository = repository
    app.state.access_control = access
    app.state.cache = cache
    app.state.notifications = notifications
    app.state.ticket_service = TicketService(
        repository=repository, access=access, cache=cache, notifications=notifications
    )
    logger.info(
        "Dependencies ready: storage=%s cache=%s notifications=%s",
        type(repository).__name__,
        cache.backend.name,
        "on" if notifications.enabled else "off",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    bootstrap(app)
    logger.info("Ticket service started (storage=%s)", type(app.state.repository).__name__)
    yield
    if hasattr(app.state.repository, "db"):
        app.state.repository.db.dispose()
    logging.shutdown()
    logger.info("Ticket service stopped")


def _openapi_servers(settings: Settings) -> list[dict]:
    """Список servers для Swagger: публичный URL (если задан) + локальные."""
    servers: list[dict] = []
    if settings.public_base_url.strip():
        servers.append(
            {
                "url": settings.public_base_url.strip().rstrip("/"),
                "description": "Публичный URL (VPS / прод)",
            }
        )
    servers.extend(
        [
            {"url": "http://localhost:8080", "description": "Локальная разработка"},
            {"url": "http://ticket-service:8080", "description": "Docker-сеть (docker-compose)"},
        ]
    )
    return servers


def _parse_cors_origins(raw: str) -> list[str]:
    """``CORS_ORIGINS``: comma-separated list или ``*``."""
    value = (raw or "").strip()
    if not value or value == "*":
        return ["*"]
    return [part.strip() for part in value.split(",") if part.strip()]


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Фабрика приложения (используется run.py и тестами)."""
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=OPENAPI_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        contact={"name": "Ticket Service Team", "email": "support@example.com"},
        license_info={"name": "MIT License"},
        servers=_openapi_servers(settings),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True},
        openapi_url="/openapi.json",
    )

    origins = _parse_cors_origins(settings.cors_origins)
    # credentials + "*" нельзя сочетать по спецификации браузера
    allow_credentials = settings.cors_allow_credentials and origins != ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Accept-Language",
            "Content-Type",
            "Authorization",
            "X-API-Key",
            "X-User-Id",
            "X-Role",
            "X-Request-Id",
        ],
        expose_headers=["X-Request-Id"],
        max_age=600,
    )

    register_exception_handlers(app)
    register_request_logging(app)
    app.include_router(tickets_routes.router, prefix=settings.api_prefix)
    app.include_router(objects_routes.router, prefix=settings.api_prefix)
    app.include_router(users_routes.router, prefix=settings.api_prefix)
    app.include_router(notification_routes.router, prefix=settings.api_prefix)
    app.include_router(cache_routes.router, prefix=settings.api_prefix)

    def _health_payload() -> HealthResponse:
        repository = getattr(app.state, "repository", None)
        access = getattr(app.state, "access_control", None)
        cache = getattr(app.state, "cache", None)
        notifications = getattr(app.state, "notifications", None)
        db_status = "up" if repository is not None and repository.ping() else "down"
        ac_status = access.health() if access is not None else {"mode": "n/a", "available": False}
        return HealthResponse(
            status="ok" if db_status == "up" else "degraded",
            service=settings.app_name,
            version=settings.app_version,
            database=db_status,
            access_control="up" if ac_status.get("available") else "down",
            mode=f"{type(repository).__name__}/{ac_status.get('mode', 'n/a')}",
            cache=(cache.backend.name if cache is not None and cache.backend.stats().get("enabled") else "off"),
            notifications=("on" if notifications is not None and notifications.enabled else "off"),
        )

    @app.get("/health", response_model=HealthResponse, tags=["Сервис"], summary="Состояние сервиса")
    def health() -> HealthResponse:
        return _health_payload()

    @app.get(
        f"{settings.api_prefix}/health",
        response_model=HealthResponse,
        tags=["Сервис"],
        summary="Состояние сервиса (под префиксом API)",
        include_in_schema=False,
    )
    def health_api() -> HealthResponse:
        return _health_payload()

    @app.get("/", tags=["Сервис"], summary="Информация о сервисе")
    def index() -> dict:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "api": settings.api_prefix,
            "stages": ["Не обработана", "Ожидает ТО", "Диагностика", "В работе", "Контроль", "Обработана"],
        }

    customize_openapi(app)
    return app


def customize_openapi(app: FastAPI) -> None:
    """Дописывает в схему OpenAPI то, что нельзя выразить аннотациями FastAPI.

    * `securitySchemes` + глобальное требование безопасности — чтобы в Swagger UI
      появилась кнопка **Authorize** с полями X-API-Key / X-User-Id / X-Role;
    * единые ответы 401/403/404/409/422/500/503 (`ErrorResponse`) для всех операций;
    * заголовок `X-Request-Id` как параметр каждого запроса (correlation id).
    """

    def _hook() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=getattr(app, "openapi_version", "3.1.0"),
            summary="Микросервис управления заявками (CRM-задачи по предупреждениям объектов)",
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
        )
        error_ref = {"$ref": "#/components/schemas/ErrorResponse"}
        schema.setdefault("components", {}).setdefault("schemas", {})["ErrorResponse"] = {
            "type": "object",
            "title": "ErrorResponse",
            "description": "Единый формат ответа об ошибке всех эндпоинтов сервиса.",
            "required": ["error", "message"],
            "properties": {
                "error": {
                    "type": "string",
                    "description": "Машинный код ошибки",
                    "enum": [
                        "validation_error", "unauthorized", "permission_denied", "not_found",
                        "integrity_conflict", "storage_error", "access_control_unavailable",
                        "internal_error",
                    ],
                    "example": "permission_denied",
                },
                "message": {"type": "string", "description": "Читаемое описание проблемы",
                            "example": "Модуль прав доступа запретил действие 'ticket.update'"},
                "details": {"type": "object", "additionalProperties": True,
                            "description": "Контекст ошибки (action, user_id, stage и т.п.)"},
                "path": {"type": "string", "description": "Путь запроса, на котором возникла ошибка",
                         "example": "/api/v1/tickets/1"},
                "request_id": {"type": "string", "description": "Correlation id из X-Request-Id"},
            },
        }
        schema["components"]["securitySchemes"] = {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": (
                    "Ключ вызывающей системы (межсервисная аутентификация). "
                    "Значение задаётся переменной окружения TICKETS_API_KEY; "
                    "если ключ не настроен — проверка отключена (режим разработки)."
                ),
            },
            "UserIdHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "X-User-Id",
                "description": (
                    "ID пользователя, от имени которого выполняется запрос "
                    "(передаётся API-шлюзом после проверки токена). Примеры демо-режима: "
                    "admin_sidorov, manager_ivanov, engineer_kuznetsov."
                ),
            },
            "RoleHintHeader": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Role",
                "description": "Подсказка роли: ADMIN | MANAGER | ENGINEER | OBSERVER (сверяется с модулем прав доступа).",
            },
        }
        global_security = [
            {"ApiKeyAuth": [], "UserIdHeader": [], "RoleHintHeader": []}
        ]
        for path, ops in schema.get("paths", {}).items():
            for method, op in ops.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                if path.startswith("/api/"):
                    op["security"] = global_security
                responses = op.setdefault("responses", {})
                common = {
                    "401": "Отсутствует или некорректен X-API-Key / X-User-Id",
                    "403": "Модуль прав доступа запретил действие",
                    "404": "Заявка или пользователь не найдены",
                    "409": "Конфликт данных (нарушение целостности)",
                    "422": "Ошибка валидации схемы запроса",
                    "500": "Внутренняя ошибка сервиса",
                    "503": "Хранилище или модуль прав доступа недоступны",
                }
                applicable = ["401", "403", "422", "500", "503"]
                if method != "post" or "/tickets" not in path:
                    applicable.append("404")
                if method in ("patch", "put", "post", "delete"):
                    applicable.append("409")
                for code in applicable:
                    responses.setdefault(code, {"description": common[code],
                                                "content": {"application/json": {"schema": error_ref}}})
                op.setdefault("parameters", []).append({
                    "name": "X-Request-Id",
                    "in": "header",
                    "required": False,
                    "description": "Correlation id вызывающей стороны (сервер вернёт его же в ответе).",
                    "schema": {"type": "string", "maxLength": 64},
                })
        app.openapi_schema = schema
        return schema

    app.openapi = _hook


def register_request_logging(app: FastAPI) -> None:
    """Middleware логирования HTTP-запросов: correlation-id + длительность.

    ``X-Request-Id`` принимается от шлюза либо генерируется; возвращается в
    ответе и попадает в каждую строку лога запроса (удобно для grep/ELK).
    """
    access_logger = logging.getLogger("ticket_service.access")

    @app.middleware("http")
    async def _log_requests(request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            access_logger.exception(
                "request_id=%s %s %s FAILED after %.1f ms", request_id,
                request.method, request.url.path, elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-Id"] = request_id
        log = access_logger.warning if response.status_code >= 500 else access_logger.info
        log(
            "request_id=%s %s %s -> %s in %.1f ms",
            request_id, request.method, request.url.path, response.status_code, elapsed_ms,
        )
        return response


def register_exception_handlers(app: FastAPI) -> None:
    """Единая обработка ошибок: доменные исключения и системные сбои.

    Все ответы об ошибке имеют один формат ``{error, message, details, path}``
    (см. ``app.models.ErrorResponse``), чтобы клиент мог разбирать их программно.
    """

    def _payload(exc: TicketServiceError, request: Request) -> dict:
        data = {
            "error": exc.code,
            "message": exc.message,
            "details": exc.details,
            "path": request.url.path,
        }
        request_id = getattr(request.state, "request_id", None)
        if request_id:
            data["request_id"] = request_id
        return data

    @app.exception_handler(TicketServiceError)
    async def _ticket_error_handler(request: Request, exc: TicketServiceError) -> JSONResponse:
        if exc.http_status >= 500:
            logger.error("%s: %s (%s)", exc.code, exc.message, exc.details)
        else:
            logger.warning("%s: %s (%s)", exc.code, exc.message, exc.details)
        return JSONResponse(status_code=exc.http_status, content=_payload(exc, request))

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """422 от Pydantic приводим к общему формату + читаемое резюме на русском."""
        errors = []
        for err in exc.errors():
            loc = [str(part) for part in err.get("loc", ()) if part != "body"]
            errors.append(
                {
                    "field": ".".join(loc) or "-",
                    "type": err.get("type", "invalid"),
                    "message": err.get("msg", "некорректное значение"),
                }
            )
        summary = "; ".join(f"{e['field']}: {e['message']}" for e in errors) or "ошибка валидации запроса"
        logger.warning("Validation failed for %s: %s", request.url.path, summary)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "validation_error",
                "message": f"Запрос не прошёл валидацию: {summary}",
                "details": {"errors": errors},
                "path": request.url.path,
            },
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
        """Нарушение ограничений БД (CHECK/UNIQUE/FK), если оно дошло до API."""
        reason = str(getattr(exc, "orig", exc)).splitlines()[0][:300]
        logger.error("Integrity error on %s: %s", request.url.path, reason)
        return JSONResponse(
            status_code=409,
            content={
                "error": "integrity_conflict",
                "message": "Данные нарушают ограничения целостности хранилища",
                "details": {"reason": reason},
                "path": request.url.path,
            },
        )

    @app.exception_handler(DBAPIError)  # включает подклассы OperationalError/IntegrityError
    async def _storage_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """PostgreSQL недоступен / отсутствует таблица / таймаут пула."""
        reason = str(getattr(exc, "orig", exc)).splitlines()[0][:300]
        logger.error("Storage error on %s: %s", request.url.path, reason)
        return JSONResponse(
            status_code=503,
            content={
                "error": "storage_error",
                "message": "Хранилище данных временно недоступно; повторите запрос позже",
                "details": {"reason": reason},
                "path": request.url.path,
            },
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Финальный страховочный обработчик: не отдаём наружу трейсбек."""
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "Внутренняя ошибка сервиса; обратитесь к администратору, указав request_id",
                "details": {"exception": type(exc).__name__},
                "path": request.url.path,
                "request_id": getattr(request.state, "request_id", None),
            },
        )


app = create_app()
