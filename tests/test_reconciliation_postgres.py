import asyncio
import os
from datetime import UTC, datetime, timedelta
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
from app.models.reconciliation import BillingException, MessageReconciliationAttempt
from app.models.whatsapp_outbound_message import OutboundMessage
from app.services.reconciliation import (
    InvalidReconciliationTransitionError,
    ReconciliationService,
    ReconciliationTooRecentError,
)
from app.services.whatsapp_webhook import WhatsAppWebhookService

pytestmark = pytest.mark.asyncio


async def database_sessions():
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
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def create_uncertain(sessions, *, balance=100, amount=60, provider_id=None):
    application_id = uuid4()
    account_id = uuid4()
    message_id = uuid4()
    rule_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            MessagingApplication(
                id=application_id,
                name="Reconciliation race test",
                slug=f"reconciliation-{uuid4().hex}",
            )
        )
        await session.flush()
        session.add(
            BillingAccount(
                id=account_id,
                application_id=application_id,
                external_id=f"school-{uuid4().hex}",
                name="Test School",
                currency="KES",
                balance_minor=balance,
                reserved_minor=amount,
            )
        )
        session.add(
            PricingRule(
                id=rule_id,
                application_id=application_id,
                channel="whatsapp",
                message_kind="text",
                currency="KES",
                customer_price_minor=amount,
                effective_from=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            OutboundMessage(
                id=message_id,
                application_id=application_id,
                billing_account_id=account_id,
                channel="whatsapp",
                recipient="254700000001",
                message_kind="text",
                text_body="test-only",
                provider="meta",
                provider_message_id=provider_id,
                status="uncertain",
                idempotency_key=f"reconciliation-{uuid4().hex}",
                payload_hash="0" * 64,
            )
        )
        await session.flush()
        session.add_all(
            [
                BillingReservation(
                    billing_account_id=account_id,
                    outbound_message_id=message_id,
                    pricing_rule_id=rule_id,
                    amount_minor=amount,
                    currency="KES",
                    created_at=datetime.now(UTC) - timedelta(days=2),
                ),
                WalletTransaction(
                    billing_account_id=account_id,
                    type="credit",
                    amount_minor=balance,
                    currency="KES",
                    reference_type="test_setup",
                    reference_id=str(message_id),
                ),
            ]
        )
    return application_id, account_id, message_id


async def cleanup(sessions, application_id):
    async with sessions.begin() as session:
        message_ids = select(OutboundMessage.id).where(
            OutboundMessage.application_id == application_id
        )
        account_ids = select(BillingAccount.id).where(
            BillingAccount.application_id == application_id
        )
        await session.execute(
            delete(MessageReconciliationAttempt).where(
                MessageReconciliationAttempt.outbound_message_id.in_(message_ids)
            )
        )
        await session.execute(
            delete(BillingException).where(BillingException.outbound_message_id.in_(message_ids))
        )
        await session.execute(
            delete(MessageUsage).where(MessageUsage.application_id == application_id)
        )
        await session.execute(
            delete(WalletTransaction).where(WalletTransaction.billing_account_id.in_(account_ids))
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
        await session.execute(
            delete(BillingAccount).where(BillingAccount.application_id == application_id)
        )
        await session.execute(
            delete(MessagingApplication).where(MessagingApplication.id == application_id)
        )


async def counts(sessions, account_id, message_id):
    async with sessions() as session:
        account = await session.get(BillingAccount, account_id)
        usage = await session.scalar(
            select(func.count())
            .select_from(MessageUsage)
            .where(MessageUsage.outbound_message_id == message_id)
        )
        debits = await session.scalar(
            select(func.count())
            .select_from(WalletTransaction)
            .where(
                WalletTransaction.reference_type == "outbound_message",
                WalletTransaction.reference_id == str(message_id),
            )
        )
        return account, usage, debits


async def test_uncertain_listing_default_stale_filter_and_application_isolation() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, old_message_id = await create_uncertain(
        sessions, balance=300, amount=60
    )
    other_application_id = None
    try:
        recent_message_id = uuid4()
        now = datetime.now(UTC)
        async with sessions.begin() as session:
            old_message = await session.get(OutboundMessage, old_message_id)
            account = await session.get(BillingAccount, account_id)
            rule = await session.scalar(
                select(PricingRule).where(PricingRule.application_id == application_id)
            )
            old_message.created_at = now - timedelta(days=2)
            account.reserved_minor += 60
            session.add(
                OutboundMessage(
                    id=recent_message_id,
                    application_id=application_id,
                    billing_account_id=account_id,
                    channel="whatsapp",
                    recipient="254700000001",
                    message_kind="text",
                    text_body="test-only",
                    provider="meta",
                    status="uncertain",
                    idempotency_key=f"recent-{uuid4().hex}",
                    payload_hash="1" * 64,
                    created_at=now,
                )
            )
            await session.flush()
            session.add(
                BillingReservation(
                    billing_account_id=account_id,
                    outbound_message_id=recent_message_id,
                    pricing_rule_id=rule.id,
                    amount_minor=60,
                    currency="KES",
                    created_at=now,
                )
            )

        other_application_id, _, other_message_id = await create_uncertain(sessions)
        async with sessions() as session:
            service = ReconciliationService()
            all_rows = await service.list_uncertain(session, application_id, limit=50, offset=0)
            stale_rows = await service.list_stale(
                session,
                application_id,
                older_than=now - timedelta(days=1),
                limit=50,
                offset=0,
            )
        assert [row[0].id for row in all_rows] == [old_message_id, recent_message_id]
        assert [row[0].id for row in stale_rows] == [old_message_id]
        assert other_message_id not in {row[0].id for row in all_rows}
    finally:
        await cleanup(sessions, application_id)
        if other_application_id is not None:
            await cleanup(sessions, other_application_id)
        await engine.dispose()


async def test_two_acceptance_reconciliations_charge_once() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id = await create_uncertain(sessions)

    async def accept():
        async with sessions() as session:
            return await ReconciliationService().reconcile(
                session,
                application_id=application_id,
                message_id=message_id,
                outcome="accepted",
                reason="operator_confirmed",
                provider_message_id="wamid.reconciliation.concurrent",
            )

    try:
        await asyncio.gather(accept(), accept())
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert account is not None
        assert (account.balance_minor, account.reserved_minor) == (40, 0)
        assert (usage, debits) == (1, 1)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_webhook_and_operator_reconciliation_race_charge_once() -> None:
    provider_id = "wamid.reconciliation.webhook-race"
    engine, sessions = await database_sessions()
    application_id, account_id, message_id = await create_uncertain(
        sessions, provider_id=provider_id
    )

    async def operator():
        async with sessions() as session:
            return await ReconciliationService().reconcile(
                session,
                application_id=application_id,
                message_id=message_id,
                outcome="accepted",
                reason="operator_confirmed",
                provider_message_id=provider_id,
            )

    async def webhook():
        async with sessions() as session, session.begin():
            await WhatsAppWebhookService()._reconcile_statuses(
                session, [{"id": provider_id, "status": "delivered", "timestamp": "1788120000"}]
            )

    try:
        await asyncio.gather(operator(), webhook())
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert account is not None
        assert (account.balance_minor, account.reserved_minor) == (40, 0)
        assert (usage, debits) == (1, 1)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_release_and_acceptance_race_has_one_consistent_outcome() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id = await create_uncertain(sessions)

    async def release():
        async with sessions() as session:
            return await ReconciliationService().release_stale(
                session,
                application_id=application_id,
                message_id=message_id,
                reason="operator_abandoned",
                threshold_minutes=1,
                force=False,
            )

    async def accept():
        async with sessions() as session:
            return await ReconciliationService().reconcile(
                session,
                application_id=application_id,
                message_id=message_id,
                outcome="accepted",
                reason="provider_confirmed",
                provider_message_id="wamid.reconciliation.release-race",
            )

    try:
        results = await asyncio.gather(release(), accept(), return_exceptions=True)
        assert sum(isinstance(item, InvalidReconciliationTransitionError) for item in results) <= 1
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert account is not None
        if usage == 1:
            assert (account.balance_minor, account.reserved_minor, debits) == (40, 0, 1)
        else:
            assert (account.balance_minor, account.reserved_minor, debits) == (100, 0, 0)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_unknown_rejection_and_stale_release_financial_states() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id = await create_uncertain(sessions)
    service = ReconciliationService()
    try:
        async with sessions() as session:
            await service.reconcile(
                session,
                application_id=application_id,
                message_id=message_id,
                outcome="unknown",
                reason="provider_lookup_unavailable",
            )
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert account is not None
        assert (account.balance_minor, account.reserved_minor, usage, debits) == (100, 60, 0, 0)

        async with sessions.begin() as session:
            reservation = await session.scalar(
                select(BillingReservation).where(
                    BillingReservation.outbound_message_id == message_id
                )
            )
            assert reservation is not None
            reservation.created_at = datetime.now(UTC)
        with pytest.raises(ReconciliationTooRecentError):
            async with sessions() as session:
                await service.release_stale(
                    session,
                    application_id=application_id,
                    message_id=message_id,
                    reason="too_recent",
                    threshold_minutes=1440,
                    force=False,
                )
        async with sessions() as session:
            await service.release_stale(
                session,
                application_id=application_id,
                message_id=message_id,
                reason="forced_after_review",
                threshold_minutes=1440,
                force=True,
            )
        async with sessions() as session:
            await service.release_stale(
                session,
                application_id=application_id,
                message_id=message_id,
                reason="idempotent_replay",
                threshold_minutes=1440,
                force=True,
            )
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert account is not None
        assert (account.balance_minor, account.reserved_minor, usage, debits) == (100, 0, 0, 0)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_late_callback_after_release_creates_exception_when_funds_unavailable() -> None:
    provider_id = "wamid.reconciliation.late-after-release"
    engine, sessions = await database_sessions()
    application_id, account_id, message_id = await create_uncertain(sessions)
    try:
        async with sessions() as session:
            await ReconciliationService().release_stale(
                session,
                application_id=application_id,
                message_id=message_id,
                reason="operator_could_not_confirm",
                threshold_minutes=1,
                force=False,
            )
        async with sessions.begin() as session:
            account = await session.get(BillingAccount, account_id, with_for_update=True)
            message = await session.get(OutboundMessage, message_id)
            assert account is not None and message is not None
            account.balance_minor -= 50
            message.provider_message_id = provider_id
            session.add(
                WalletTransaction(
                    billing_account_id=account_id,
                    type="adjustment_debit",
                    amount_minor=50,
                    currency="KES",
                    reference_type="test_other_spend",
                    reference_id=str(uuid4()),
                )
            )
        async with sessions() as session, session.begin():
            await WhatsAppWebhookService()._reconcile_statuses(
                session, [{"id": provider_id, "status": "delivered", "timestamp": "1788120000"}]
            )
        account, usage, debits = await counts(sessions, account_id, message_id)
        async with sessions() as session:
            exception_count = await session.scalar(
                select(func.count())
                .select_from(BillingException)
                .where(
                    BillingException.outbound_message_id == message_id,
                    BillingException.status == "open",
                )
            )
            message = await session.get(OutboundMessage, message_id)
        assert account is not None and message is not None
        assert (account.balance_minor, account.reserved_minor, usage, debits) == (50, 0, 0, 0)
        assert exception_count == 1
        assert message.status == "delivered"
        assert message.reconciliation_status == "billing_exception"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_confirmed_rejection_releases_without_charge() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id = await create_uncertain(sessions)
    try:
        async with sessions() as session:
            message = await ReconciliationService().reconcile(
                session,
                application_id=application_id,
                message_id=message_id,
                outcome="rejected",
                reason="provider_rejection_confirmed",
            )
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert account is not None
        assert message.status == "failed"
        assert message.reconciliation_status == "resolved_rejected"
        assert (account.balance_minor, account.reserved_minor, usage, debits) == (100, 0, 0, 0)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()
