"""Тесты бизнес-логики: валидация создания заявки, обработка ошибок, кэш.

Запускаются без PostgreSQL (in-memory хранилище + in-memory модуль прав доступа),
поэтому подходят и для CI, и для локальной проверки: ``pytest -q``.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

os.environ.setdefault("TICKETS_DATABASE_URL", "memory")
os.environ.setdefault("SEED_DEMO_DATA", "1")
os.environ.setdefault("CACHE_ENABLED", "1")
os.environ.setdefault("LOG_FILE", "")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError as PydValidationError  # noqa: E402

from app.config import Settings  # noqa: E402
from app.exceptions import NotFoundError, PermissionDeniedError, ValidationError  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import (  # noqa: E402
    ActionType,
    ActorContext,
    Role,
    TicketCreateRequest,
    TicketStage,
)
from app.repositories.ticket_repository import InMemoryTicketRepository  # noqa: E402
from app.services.notification_service import NotificationService  # noqa: E402
from app.services.ticket_service import TicketService  # noqa: E402

API_KEY = "gateway-secret-key"


def make_actor(user_id: str = "manager_ivanov", role: Role = Role.MANAGER) -> ActorContext:
    return ActorContext(
        user_id=user_id,
        full_name="Тест",
        role=role,
        roles=[role],
        object_scope=["*"],
        allowed_actions=["*"],
    )


def build_service(**settings_overrides):
    """Собирает сервис на in-memory хранилище + встроенном модуле прав доступа."""
    from app.auth.access_control import DEMO_USERS, InMemoryAccessControlClient

    settings = Settings(
        database_url="memory",
        tickets_api_key="",
        access_control_url="",
        **settings_overrides,
    )
    repo = InMemoryTicketRepository()
    access = InMemoryAccessControlClient()
    repo.seed_users(DEMO_USERS)
    notifications = NotificationService(repository=repo, settings=settings)
    return TicketService(
        repository=repo, access=access, notifications=notifications, settings=settings
    )


def valid_payload(**overrides) -> dict:
    base = {
        "title": "Плановое ТО насоса ЦНС-180",
        "description": "Заменить уплотнение, проверить вибрацию",
        "object_id": "OBJ-101",
        "assignee_ids": ["engineer_kuznetsov"],
        "due_date": (date.today() + timedelta(days=7)).isoformat(),
        "warning_source": "SCADA: high vibration",
    }
    base.update(overrides)
    return base


# =============================================================================
# 1. Валидация при создании заявки
# =============================================================================
class TestCreateValidation:
    def test_valid_request_is_accepted(self):
        service = build_service()
        dto = service.create_ticket(TicketCreateRequest(**valid_payload()), make_actor())
        assert dto.id > 0
        assert dto.stage is TicketStage.UNPROCESSED
        assert dto.object_id == "OBJ-101"
        assert dto.assignees[0].user_id == "engineer_kuznetsov"

    @pytest.mark.parametrize("missing", ["title", "object_id"])
    def test_required_fields(self, missing):
        payload = valid_payload()
        payload.pop(missing)
        with pytest.raises(PydValidationError) as info:
            TicketCreateRequest(**payload)
        assert any(err["loc"][0] == missing for err in info.value.errors())

    @pytest.mark.parametrize("bad_title", ["", "   ", "ab", "a" * 300])
    def test_title_length_and_blank(self, bad_title):
        with pytest.raises(PydValidationError):
            TicketCreateRequest(**valid_payload(title=bad_title))

    def test_object_id_blank_rejected(self):
        with pytest.raises(PydValidationError):
            TicketCreateRequest(**valid_payload(object_id="   "))

    def test_due_date_in_past_rejected(self):
        with pytest.raises(PydValidationError) as info:
            TicketCreateRequest(**valid_payload(due_date=(date.today() - timedelta(days=1)).isoformat()))
        assert "прошлом" in str(info.value)

    def test_due_date_too_far_rejected(self):
        with pytest.raises(PydValidationError):
            TicketCreateRequest(**valid_payload(due_date=(date.today() + timedelta(days=365 * 9)).isoformat()))

    def test_created_at_in_future_rejected(self):
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        with pytest.raises(PydValidationError) as info:
            TicketCreateRequest(**valid_payload(created_at=future))
        assert "будущем" in str(info.value)

    def test_due_date_before_created_at_rejected(self):
        created = datetime(2026, 1, 10, tzinfo=timezone.utc)
        with pytest.raises(PydValidationError) as info:
            TicketCreateRequest(**valid_payload(created_at=created, due_date=date(2026, 1, 5)))
        assert "раньше даты постановки" in str(info.value)

    def test_custom_created_at_in_past_is_allowed(self):
        """Можно завести заявку задним числом (по давнему предупреждению)."""
        service = build_service()
        created = datetime.now(timezone.utc) - timedelta(days=3)
        dto = service.create_ticket(
            TicketCreateRequest(**valid_payload(created_at=created)), make_actor()
        )
        assert dto.created_at.date() == created.date()

    def test_assignee_and_watcher_overlap_rejected(self):
        with pytest.raises(PydValidationError) as info:
            TicketCreateRequest(
                **valid_payload(assignee_ids=["u1"], watcher_ids=["u1"])
            )
        assert "исполнителем и наблюдателем" in str(info.value)

    def test_duplicate_people_are_deduplicated(self):
        service = build_service()
        dto = service.create_ticket(
            TicketCreateRequest(
                **valid_payload(assignee_ids=["engineer_kuznetsov", "engineer_kuznetsov"])
            ),
            make_actor(),
        )
        assert len(dto.assignees) == 1

    def test_at_least_one_assignee_required(self):
        service = build_service()
        with pytest.raises(ValidationError) as info:
            service.create_ticket(TicketCreateRequest(**valid_payload(assignee_ids=[])), make_actor())
        assert "исполнитель" in info.value.message

    def test_unknown_user_rejected(self):
        service = build_service()
        with pytest.raises(ValidationError) as info:
            service.create_ticket(
                TicketCreateRequest(**valid_payload(assignee_ids=["ghost_user"])), make_actor()
            )
        assert "ghost_user" in info.value.message

    def test_author_cannot_be_own_assignee(self):
        service = build_service()
        with pytest.raises(ValidationError) as info:
            service.create_ticket(
                TicketCreateRequest(
                    **valid_payload(author_id="manager_ivanov", assignee_ids=["manager_ivanov"])
                ),
                make_actor(),
            )
        assert "Постановщик" in info.value.message

    def test_new_ticket_starts_unprocessed_for_manager(self):
        service = build_service()
        with pytest.raises(ValidationError):
            service.create_ticket(
                TicketCreateRequest(**valid_payload(stage=TicketStage.IN_PROGRESS)), make_actor()
            )

    def test_description_max_length(self):
        with pytest.raises(PydValidationError):
            TicketCreateRequest(**valid_payload(description="x" * 9000))


# =============================================================================
# 2. Обработка ошибок / доменные исключения
# =============================================================================
class TestErrors:
    def test_not_found(self):
        service = build_service()
        with pytest.raises(NotFoundError):
            service.get_ticket(999, make_actor())

    def test_permission_denied_for_foreign_object(self):
        service = build_service()
        actor = ActorContext(
            user_id="engineer_kuznetsov",
            role=Role.ENGINEER,
            roles=[Role.ENGINEER],
            object_scope=["OBJ-200"],
            allowed_actions=[ActionType.TICKET_READ.value],
        )
        dto = service.create_ticket(TicketCreateRequest(**valid_payload()), make_actor())
        with pytest.raises(PermissionDeniedError):
            service.get_ticket(dto.id, actor)

    def test_invalid_stage_transition(self):
        from app.exceptions import StageTransitionError
        from app.models import StageChangeRequest

        service = build_service()
        dto = service.create_ticket(TicketCreateRequest(**valid_payload()), make_actor())
        with pytest.raises(StageTransitionError) as info:
            service.change_stage(
                dto.id,
                StageChangeRequest(stage=TicketStage.PROCESSED),
                make_actor(),
            )
        assert info.value.code == "invalid_stage_transition"
        assert info.value.http_status == 400

    def test_empty_update_rejected(self):
        service = build_service()
        dto = service.create_ticket(TicketCreateRequest(**valid_payload()), make_actor())
        from app.models import TicketUpdateRequest

        with pytest.raises(ValidationError):
            service.update_ticket(dto.id, TicketUpdateRequest(), make_actor())

    def test_update_preserves_date_consistency(self):
        service = build_service()
        # заявка «задним числом»: создана в прошлом, срок - позже даты постановки
        created = datetime.now(timezone.utc) - timedelta(days=60)
        dto = service.create_ticket(
            TicketCreateRequest(
                **valid_payload(created_at=created, due_date=date.today() + timedelta(days=5))
            ),
            make_actor(),
        )
        from app.models import TicketUpdateRequest

        # схема не пустит срок в прошлое (422) ...
        with pytest.raises(PydValidationError):
            TicketUpdateRequest(due_date=(date.today() - timedelta(days=1)).isoformat())
        # ... а сервис не даст поставить срок раньше даты постановки заявки
        with pytest.raises(ValidationError) as info:
            service.update_ticket(dto.id, TicketUpdateRequest(due_date=date.today()), make_actor())
        assert "раньше даты постановки" in info.value.message
        assert info.value.code == "validation_error"

    def test_storage_failure_maps_to_domain_error(self):
        """Сбой хранилища не должен утекать как «сырое» исключение драйвера."""
        from sqlalchemy.exc import OperationalError

        from app.exceptions import StorageError

        service = build_service()

        class BrokenRepo(InMemoryTicketRepository):
            def create(self, data):  # noqa: ANN001
                raise OperationalError("INSERT INTO tickets", {}, Exception("connection to server failed"))

        service.repo = BrokenRepo()
        with pytest.raises(StorageError) as info:
            service.create_ticket(TicketCreateRequest(**valid_payload()), make_actor())
        assert info.value.http_status == 503
        assert info.value.code == "storage_error"

    def test_integrity_failure_maps_to_conflict(self):
        from sqlalchemy.exc import IntegrityError

        from app.exceptions import IntegrityConflictError

        service = build_service()

        class BadRepo(InMemoryTicketRepository):
            def create(self, data):  # noqa: ANN001
                raise IntegrityError(
                    "INSERT", {}, Exception('duplicate key value violates unique constraint "tickets_pkey"')
                )

        service.repo = BadRepo()
        with pytest.raises(IntegrityConflictError) as info:
            service.create_ticket(TicketCreateRequest(**valid_payload()), make_actor())
        assert info.value.http_status == 409


# =============================================================================
# 3. Кеширование частых запросов
# =============================================================================
class TestCache:
    def test_ticket_card_is_cached(self):
        service = build_service()
        dto = service.create_ticket(TicketCreateRequest(**valid_payload()), make_actor())
        actor = make_actor()

        first = service.get_ticket(dto.id, actor)
        stats_after_first = service.cache_stats()
        second = service.get_ticket(dto.id, actor)
        stats_after_second = service.cache_stats()

        assert first.model_dump() == second.model_dump()
        # первый вызов - промах (заполнение кэша), второй - попадание
        assert stats_after_first["misses"] >= 1
        assert stats_after_second["hits"] > stats_after_first["hits"]

    def test_cache_invalidated_on_update(self):
        service = build_service()
        actor = make_actor()
        dto = service.create_ticket(TicketCreateRequest(**valid_payload()), actor)
        service.get_ticket(dto.id, actor)  # кладём в кэш
        from app.models import TicketUpdateRequest

        updated = service.update_ticket(dto.id, TicketUpdateRequest(title="Новое название заявки"), actor)
        assert updated.title == "Новое название заявки"
        # после инвалидации карточка читается из хранилища
        assert service.get_ticket(dto.id, actor).title == "Новое название заявки"

    def test_list_endpoint_cached(self):
        service = build_service()
        actor = make_actor()
        service.create_ticket(TicketCreateRequest(**valid_payload()), actor)
        service.list_tickets(actor, page=1, page_size=10)
        before = service.cache_stats()["entries"]
        service.list_tickets(actor, page=1, page_size=10)
        after = service.cache_stats()
        assert after["entries"] == before
        assert after["hits"] >= 1

    def test_disabled_cache_is_noop(self):
        service = build_service(cache_enabled=False)
        service.cache.clear()
        assert service.cache_stats()["enabled"] is False


# =============================================================================
# 4. HTTP-слой: единый формат ошибок, уведомления, кэш-эндпоинты
# =============================================================================
@pytest.fixture(scope="module")
def client():
    app = create_app(
        Settings(
            database_url="memory",
            seed_demo_data=True,
            tickets_api_key=API_KEY,
            access_control_url="",
        )
    )
    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


HEADERS = {"X-API-Key": API_KEY, "X-User-Id": "manager_ivanov", "X-Role": "MANAGER"}


class TestHttpApi:
    def test_health_reports_cache_and_notifications(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["database"] == "up"
        assert body["cache"] in ("memory", "redis", "off")
        assert body["notifications"] in ("on", "off")

    def test_create_validation_error_format(self, client):
        response = client.post(
            "/api/v1/tickets",
            headers=HEADERS,
            json={"title": "ab", "object_id": "", "assignee_ids": []},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "validation_error"
        assert "message" in body and "details" in body
        fields = {e["field"] for e in body["details"]["errors"]}
        assert "title" in fields and "object_id" in fields

    def test_create_business_error_format(self, client):
        response = client.post(
            "/api/v1/tickets",
            headers=HEADERS,
            json=valid_payload(due_date=(date.today() - timedelta(days=5)).isoformat()),
        )
        assert response.status_code == 422
        assert "прошлом" in response.json()["message"]

    def test_created_ticket_roundtrip(self, client):
        created = client.post("/api/v1/tickets", headers=HEADERS, json=valid_payload())
        assert created.status_code == 201, created.text
        ticket_id = created.json()["id"]
        fetched = client.get(f"/api/v1/tickets/{ticket_id}", headers=HEADERS)
        assert fetched.status_code == 200
        assert fetched.json()["stage_title"] == "Не обработана"

    def test_missing_api_key_is_401(self, client):
        response = client.get("/api/v1/tickets", headers={"X-User-Id": "manager_ivanov"})
        assert response.status_code == 401
        assert response.json()["detail"]

    def test_not_found_404_body(self, client):
        response = client.get("/api/v1/tickets/424242", headers=HEADERS)
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"

    def test_cache_stats_endpoint(self, client):
        response = client.get("/api/v1/cache/stats", headers=HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert body["backend"] in ("memory", "noop", "redis")
        assert "hits" in body and "misses" in body

    def test_cache_clear_requires_admin(self, client):
        denied = client.post("/api/v1/cache/clear", headers=HEADERS)
        assert denied.status_code == 403
        admin_headers = {"X-API-Key": API_KEY, "X-User-Id": "admin_sidorov", "X-Role": "ADMIN"}
        allowed = client.post("/api/v1/cache/clear", headers=admin_headers)
        assert allowed.status_code == 200
        assert allowed.json()["details"]["cleared"] is True

    def test_notifications_endpoints_exist(self, client):
        created = client.post("/api/v1/tickets", headers=HEADERS, json=valid_payload()).json()
        mine = client.get("/api/v1/notifications", headers=HEADERS)
        assert mine.status_code == 200
        by_ticket = client.get(f"/api/v1/tickets/{created['id']}/notifications", headers=HEADERS)
        assert by_ticket.status_code == 200
        assert isinstance(by_ticket.json(), list)

    def test_history_written_for_audit(self, client):
        created = client.post("/api/v1/tickets", headers=HEADERS, json=valid_payload()).json()
        history = client.get(f"/api/v1/tickets/{created['id']}/history", headers=HEADERS).json()
        assert history[0]["action"] == "CREATED"
