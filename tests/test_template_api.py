from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_authenticated_application
from app.api.routes.messages import get_messaging_service
from app.db.session import get_db_session
from app.services.messaging import (
    IdempotencyConflictError,
    TemplateNotFoundError,
    TemplateParameterError,
    TemplateUnavailableError,
)


class Service:
    def __init__(self, error=None):
        self.error = error

    async def send_template(self, session, application, idempotency_key, body):
        if self.error:
            raise self.error
        return SimpleNamespace(
            id=uuid4(),
            channel="whatsapp",
            status="sent",
            recipient=body.to,
            message_kind="template",
            template_name=body.template,
            provider_message_id="wamid.template",
            source_type=None,
            source_id=None,
            message_metadata=body.metadata,
            created_at=datetime.now(UTC),
            sent_at=datetime.now(UTC),
            delivered_at=None,
            read_at=None,
            failed_at=None,
        )


async def call(app, service, body=None, headers=None):
    async def session():
        yield SimpleNamespace()

    app.dependency_overrides[get_authenticated_application] = lambda: SimpleNamespace(
        id=uuid4(), slug="school-management"
    )
    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_messaging_service] = lambda: service
    request_body = body or {
        "channel": "whatsapp",
        "to": "254700000001",
        "template": "student_results_ready",
        "parameters": {},
    }
    request_headers = {"Idempotency-Key": "template-test-001", **(headers or {})}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/v1/messages/template", json=request_body, headers=request_headers
        )


@pytest.mark.asyncio
async def test_template_api_success(app) -> None:
    response = await call(app, Service())
    assert response.status_code == 200
    assert response.json()["template"] == "student_results_ready"
    assert response.json()["message_kind"] == "template"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (TemplateNotFoundError(), 404),
        (TemplateUnavailableError(), 409),
        (TemplateParameterError("missing parameters: term"), 422),
        (IdempotencyConflictError(), 409),
    ],
)
async def test_template_api_maps_domain_errors(app, error, status_code) -> None:
    response = await call(app, Service(error))
    assert response.status_code == status_code


@pytest.mark.asyncio
async def test_template_api_requires_idempotency_key_and_valid_recipient(app) -> None:
    missing_key = await call(app, Service(), headers={"Idempotency-Key": ""})
    assert missing_key.status_code == 400
    invalid_recipient = await call(
        app,
        Service(),
        body={
            "channel": "whatsapp",
            "to": "invalid",
            "template": "student_results_ready",
            "parameters": {},
        },
    )
    assert invalid_recipient.status_code == 422
