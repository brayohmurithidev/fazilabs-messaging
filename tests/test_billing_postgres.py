import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.billing import (
    BillingAccount,
    BillingReservation,
    MessageUsage,
    PricingRule,
    WalletTransaction,
)
from app.models.messaging_application import MessagingApplication
from app.models.whatsapp_outbound_message import OutboundMessage
from app.schemas.messages import TextMessageRequest
from app.schemas.whatsapp import WhatsAppSendResult
from app.services.billing import InsufficientBalanceError
from app.services.messaging import MessagingService

pytestmark = pytest.mark.asyncio


class Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def send_text_message(self, recipient, text):
        self.calls += 1
        await asyncio.sleep(0)
        return WhatsAppSendResult(
            recipient=recipient, meta_message_id=f"wamid.billing.{uuid4()}", success=True
        )


async def test_concurrent_prepaid_reservations_cannot_overspend() -> None:
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
    application_id = uuid4()
    account_id = uuid4()
    provider = Provider()
    application = MessagingApplication(
        id=application_id,
        name="Billing concurrency test",
        slug=f"billing-{uuid4().hex}",
    )
    account = BillingAccount(
        id=account_id,
        application_id=application_id,
        external_id="school-concurrency",
        name="Concurrency School",
        currency="KES",
        balance_minor=100,
    )
    rule = PricingRule(
        application_id=application_id,
        channel="whatsapp",
        message_kind="text",
        billing_category=None,
        currency="KES",
        customer_price_minor=60,
        effective_from=datetime.now(UTC),
    )
    request = TextMessageRequest(
        channel="whatsapp",
        to="254700000001",
        billing_account="school-concurrency",
        text="test-only message",
    )

    async def send(key: str):
        async with sessions() as session:
            return await MessagingService(provider).send_text(session, application, key, request)

    try:
        async with sessions.begin() as session:
            session.add(application)
            await session.flush()
            session.add_all([account, rule])
        results = await asyncio.gather(
            send("billing-concurrent-001"),
            send("billing-concurrent-002"),
            return_exceptions=True,
        )
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, InsufficientBalanceError) for result in results) == 1
        assert provider.calls == 1
        async with sessions() as session:
            refreshed = await session.get(BillingAccount, account_id)
            usage_count = await session.scalar(
                select(func.count())
                .select_from(MessageUsage)
                .where(MessageUsage.billing_account_id == account_id)
            )
            debit_count = await session.scalar(
                select(func.count())
                .select_from(WalletTransaction)
                .where(
                    WalletTransaction.billing_account_id == account_id,
                    WalletTransaction.type == "debit",
                )
            )
        assert refreshed is not None
        assert (refreshed.balance_minor, refreshed.reserved_minor) == (40, 0)
        assert usage_count == 1
        assert debit_count == 1
    finally:
        async with sessions.begin() as session:
            message_ids = select(OutboundMessage.id).where(
                OutboundMessage.application_id == application_id
            )
            await session.execute(
                delete(MessageUsage).where(MessageUsage.application_id == application_id)
            )
            await session.execute(
                delete(WalletTransaction).where(WalletTransaction.billing_account_id == account_id)
            )
            await session.execute(
                delete(BillingReservation).where(
                    BillingReservation.outbound_message_id.in_(message_ids)
                )
            )
            await session.execute(
                delete(OutboundMessage).where(OutboundMessage.application_id == application_id)
            )
            await session.execute(
                delete(PricingRule).where(PricingRule.application_id == application_id)
            )
            await session.execute(delete(BillingAccount).where(BillingAccount.id == account_id))
            await session.execute(
                delete(MessagingApplication).where(MessagingApplication.id == application_id)
            )
        await engine.dispose()
