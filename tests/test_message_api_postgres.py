import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.messages import get_messaging_service
from app.core.api_keys import generate_api_key
from app.core.config import Environment, Settings
from app.db.session import get_db_session
from app.main import create_app
from app.models.message_template import MessageTemplate
from app.models.messaging_application import MessagingApiKey, MessagingApplication
from app.models.whatsapp_outbound_message import OutboundMessage
from app.schemas.whatsapp import WhatsAppSendResult
from app.services.messaging import MessagingService

pytestmark = pytest.mark.asyncio


class Provider:
    def __init__(self) -> None:
        self.text_calls = 0
        self.template_calls = 0

    async def send_text_message(self, recipient, text):
        self.text_calls += 1
        return WhatsAppSendResult(
            recipient=recipient, meta_message_id=f"wamid.text.{uuid4()}", success=True
        )

    async def send_template_message(self, recipient, **kwargs):
        self.template_calls += 1
        return WhatsAppSendResult(
            recipient=recipient, meta_message_id=f"wamid.template.{uuid4()}", success=True
        )


async def test_authenticated_message_endpoints_share_real_session_without_nested_begin() -> None:
    database_url = os.environ.get("APP_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("APP_TEST_DATABASE_URL is not configured")
    if "test" not in database_url.lower():
        pytest.fail("APP_TEST_DATABASE_URL must identify an isolated test database")

    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (OSError, OperationalError) as exc:
        await engine.dispose()
        pytest.skip(f"isolated PostgreSQL test database unavailable: {type(exc).__name__}")
    suffix = uuid4().hex
    raw_key, prefix, key_hash = generate_api_key()
    application_id = uuid4()
    application = MessagingApplication(
        id=application_id, name=f"Transaction test {suffix}", slug=f"tx-{suffix}"
    )
    api_key = MessagingApiKey(
        application_id=application_id,
        name="integration",
        key_prefix=prefix,
        key_hash=key_hash,
    )
    template = MessageTemplate(
        application_id=application_id,
        template_key="student_results_ready",
        channel="whatsapp",
        provider="meta",
        provider_template_name="fazi_student_results_ready_v1",
        language_code="en_US",
        parameter_schema={"body": ["parent_name", "student_name", "term"]},
    )
    provider = Provider()

    async def session_override():
        async with sessions() as session:
            yield session

    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        database_url=database_url,
        whatsapp_access_token="test-access-token",
        whatsapp_phone_number_id="123456789",
        whatsapp_api_version="vXX.X",
    )
    app = create_app(settings)
    app.dependency_overrides[get_db_session] = session_override
    app.dependency_overrides[get_messaging_service] = lambda: MessagingService(provider)
    headers = {"Authorization": f"Bearer {raw_key}"}
    template_body = {
        "channel": "whatsapp",
        "to": "254700000001",
        "template": "student_results_ready",
        "parameters": {"parent_name": "Jane", "student_name": "Brian", "term": "Term 2"},
    }

    try:
        async with sessions.begin() as session:
            session.add(application)
            await session.flush()
            session.add_all([api_key, template])

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            first = await client.post(
                "/api/v1/messages/template",
                json=template_body,
                headers={**headers, "Idempotency-Key": "template-tx-001"},
            )
            retry = await client.post(
                "/api/v1/messages/template",
                json=template_body,
                headers={**headers, "Idempotency-Key": "template-tx-001"},
            )
            changed = await client.post(
                "/api/v1/messages/template",
                json={**template_body, "to": "254700000002"},
                headers={**headers, "Idempotency-Key": "template-tx-001"},
            )
            text_response = await client.post(
                "/api/v1/messages/text",
                json={"channel": "whatsapp", "to": "254700000003", "text": "hello"},
                headers={**headers, "Idempotency-Key": "text-tx-0001"},
            )

        assert first.status_code == 200
        assert retry.status_code == 200
        assert retry.json()["id"] == first.json()["id"]
        assert changed.status_code == 409
        assert text_response.status_code == 200
        assert provider.template_calls == 1
        assert provider.text_calls == 1
        async with sessions() as session:
            template_count = await session.scalar(
                select(func.count())
                .select_from(OutboundMessage)
                .where(
                    OutboundMessage.application_id == application_id,
                    OutboundMessage.idempotency_key == "template-tx-001",
                )
            )
        assert template_count == 1
    finally:
        async with sessions.begin() as session:
            await session.execute(
                delete(OutboundMessage).where(OutboundMessage.application_id == application_id)
            )
            await session.execute(
                delete(MessagingApplication).where(MessagingApplication.id == application_id)
            )
        await engine.dispose()
