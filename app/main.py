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


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Фабрика приложения (используется run.py и тестами)."""
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Микросервис управления заявками (tasks/tickets).\n\n"
            "* Заявка создаётся по предупреждению, пришедшему **с объекта** (обязательная привязка `object_id`).\n"
            "* Жизненный цикл: Не обработана -> Ожидает ТО -> Диагностика -> В работе -> Контроль -> Обработана.\n"
            "* Права пользователей проверяет внешний **модуль прав доступа** (`ACCESS_CONTROL_URL`); "
            "здесь лишь предусмотрены точки обращения к нему.\n"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    register_exception_handlers(app)
    register_request_logging(app)
    app.include_router(tickets_routes.router, prefix=settings.api_prefix)
    app.include_router(objects_routes.router, prefix=settings.api_prefix)
    app.include_router(users_routes.router, prefix=settings.api_prefix)
    app.include_router(notification_routes.router, prefix=settings.api_prefix)
    app.include_router(cache_routes.router, prefix=settings.api_prefix)

    @app.get("/health", response_model=HealthResponse, tags=["Сервис"], summary="Состояние сервиса")
    def health() -> HealthResponse:
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

    @app.get("/", tags=["Сервис"], summary="Информация о сервисе")
    def index() -> dict:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "docs": "/docs",
            "api": settings.api_prefix,
            "stages": ["Не обработана", "Ожидает ТО", "Диагностика", "В работе", "Контроль", "Обработана"],
        }

    return app


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
