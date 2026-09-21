from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_authenticated_application
from app.api.routes.messages import get_messaging_service
from app.db.session import get_db_session
from app.services.messaging import BillingAccountRequiredError


class BillingRequiredService:
    async def send_text(self, session, application, idempotency_key, body):
        raise BillingAccountRequiredError

    async def send_template(self, session, application, idempotency_key, body):
        raise BillingAccountRequiredError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/api/v1/messages/text",
            {"channel": "whatsapp", "to": "254700000001", "text": "hello"},
        ),
        (
            "/api/v1/messages/template",
            {
                "channel": "whatsapp",
                "to": "254700000001",
                "template": "student_results_ready",
                "parameters": {},
            },
        ),
    ],
)
async def test_send_endpoints_return_stable_billing_required_error(app, path, body) -> None:
    async def session_override():
        yield SimpleNamespace()

    app.dependency_overrides[get_authenticated_application] = lambda: SimpleNamespace(
        id=uuid4(), slug="billing-required-app", billing_required=True
    )
    app.dependency_overrides[get_db_session] = session_override
    app.dependency_overrides[get_messaging_service] = BillingRequiredService
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            path, json=body, headers={"Idempotency-Key": "billing-required-001"}
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "billing_account_required",
            "message": "billing_account is required for this application.",
        }
    }
