import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.whatsapp_message import WhatsAppInboundMessage
from app.services.whatsapp_webhook import WhatsAppWebhookService
from tests.test_whatsapp_webhook import make_payload, text_message

pytestmark = pytest.mark.asyncio


async def test_postgres_idempotency_and_mixed_batch() -> None:
    """Exercise PostgreSQL JSONB, uniqueness, and ON CONFLICT when explicitly configured."""
    database_url = os.environ.get("APP_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("APP_TEST_DATABASE_URL is not configured")
    if "test" not in database_url.lower():
        pytest.fail("APP_TEST_DATABASE_URL must identify an isolated test database")

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (OSError, OperationalError) as exc:
        await engine.dispose()
        pytest.skip(f"isolated PostgreSQL test database unavailable: {type(exc).__name__}")
    first_id = f"wamid.integration.{uuid.uuid4()}"
    second_id = f"wamid.integration.{uuid.uuid4()}"
    service = WhatsAppWebhookService()

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                await service.accept_payload(make_payload(text_message(first_id)), session)
                await service.accept_payload(
                    make_payload(text_message(first_id), text_message(second_id)), session
                )
                async with session.begin():
                    count = await session.scalar(
                        select(func.count())
                        .select_from(WhatsAppInboundMessage)
                        .where(WhatsAppInboundMessage.meta_message_id.in_([first_id, second_id]))
                    )
                assert count == 2
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()
