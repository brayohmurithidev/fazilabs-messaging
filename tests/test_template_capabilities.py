from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.routes.templates import list_templates
from app.repositories.message_templates import MessageTemplateRepository


@pytest.mark.asyncio
async def test_template_capabilities_expose_semantic_billing_mode(monkeypatch) -> None:
    templates = [
        SimpleNamespace(
            template_key="student_results_ready",
            channel="whatsapp",
            language_code="en",
            parameter_schema={"body": ["parent_name"]},
            billing_mode="customer",
        ),
        SimpleNamespace(
            template_key="password_reset",
            channel="whatsapp",
            language_code="en",
            parameter_schema={"body": ["code"]},
            billing_mode="platform",
        ),
        SimpleNamespace(
            template_key="generic_notice",
            channel="sms",
            language_code=None,
            parameter_schema={"body": ["name"]},
            billing_mode="customer",
            provider_route="standard",
        ),
    ]

    async def active(self, session, application_id):
        return templates

    monkeypatch.setattr(MessageTemplateRepository, "list_active", active)
    response = await list_templates(SimpleNamespace(id=uuid4()), SimpleNamespace())

    assert [item.model_dump() for item in response.items] == [
        {
            "key": "student_results_ready",
            "channel": "whatsapp",
            "language": "en",
            "parameters": ["parent_name"],
            "billing_mode": "customer",
            "provider_route": None,
        },
        {
            "key": "password_reset",
            "channel": "whatsapp",
            "language": "en",
            "parameters": ["code"],
            "billing_mode": "platform",
            "provider_route": None,
        },
        {
            "key": "generic_notice",
            "channel": "sms",
            "language": None,
            "parameters": ["name"],
            "billing_mode": "customer",
            "provider_route": "standard",
        },
    ]
