import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.billing import (
    BillingAccount,
    MessageUsage,
    WalletTransaction,
)
from app.models.reconciliation import BillingException
from app.models.whatsapp_outbound_message import OutboundMessage
from app.services.billing import BillingService, InsufficientBalanceError
from app.services.billing_exceptions import (
    BillingExceptionInsufficientBalanceError,
    BillingExceptionNotFoundError,
    BillingExceptionResolutionConflictError,
    BillingExceptionService,
    InvalidBillingExceptionError,
)
from app.services.reconciliation import ReconciliationService
from app.services.whatsapp_webhook import WhatsAppWebhookService
from tests.test_reconciliation_postgres import cleanup, counts, create_uncertain, database_sessions

pytestmark = pytest.mark.asyncio


async def create_open_exception(sessions):
    provider_id = f"wamid.billing-exception.{uuid4().hex}"
    application_id, account_id, message_id = await create_uncertain(sessions)
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
    async with sessions() as session:
        exception_id = await session.scalar(
            select(BillingException.id).where(BillingException.outbound_message_id == message_id)
        )
    assert exception_id is not None
    return application_id, account_id, message_id, exception_id, provider_id


async def top_up(sessions, account_id, amount=20):
    async with sessions.begin() as session:
        account = await session.get(BillingAccount, account_id)
        assert account is not None
        await BillingService().credit(
            session,
            account,
            amount_minor=amount,
            currency="KES",
            reference=f"exception-test-{uuid4().hex}",
        )


async def get_exception(sessions, exception_id):
    async with sessions() as session:
        return await session.get(BillingException, exception_id)


async def test_charge_uses_snapshot_and_is_idempotent() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id, exception_id, _ = await create_open_exception(sessions)
    try:
        await top_up(sessions, account_id)
        async with sessions() as session:
            result = await BillingExceptionService().resolve(
                session,
                application_id=application_id,
                exception_id=exception_id,
                resolution="charge",
                reason="wallet_replenished",
            )
        assert result.status == "resolved_charged"
        async with sessions() as session:
            replay = await BillingExceptionService().resolve(
                session,
                application_id=application_id,
                exception_id=exception_id,
                resolution="charge",
                reason="idempotent_replay",
            )
            monthly_count, monthly_charge = (
                await session.execute(
                    select(func.count(), func.sum(MessageUsage.customer_price_minor)).where(
                        MessageUsage.billing_account_id == account_id
                    )
                )
            ).one()
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert account is not None
        assert replay.resolution_reason == "wallet_replenished"
        assert (account.balance_minor, account.reserved_minor) == (10, 0)
        assert (usage, debits, monthly_count, monthly_charge) == (1, 1, 1, 60)
        with pytest.raises(BillingExceptionResolutionConflictError):
            async with sessions() as session:
                await BillingExceptionService().resolve(
                    session,
                    application_id=application_id,
                    exception_id=exception_id,
                    resolution="waive",
                    reason="cannot_rewrite_history",
                )
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_insufficient_balance_and_waive_are_safe() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id, exception_id, _ = await create_open_exception(sessions)
    try:
        with pytest.raises(BillingExceptionInsufficientBalanceError):
            async with sessions() as session:
                await BillingExceptionService().resolve(
                    session,
                    application_id=application_id,
                    exception_id=exception_id,
                    resolution="charge",
                    reason="still_insufficient",
                )
        exception = await get_exception(sessions, exception_id)
        account, usage, debits = await counts(sessions, account_id, message_id)
        assert exception is not None and account is not None
        assert exception.status == "open"
        assert (account.balance_minor, usage, debits) == (50, 0, 0)
        async with sessions() as session:
            waived = await BillingExceptionService().resolve(
                session,
                application_id=application_id,
                exception_id=exception_id,
                resolution="waive",
                reason="customer_service_waiver",
            )
        async with sessions() as session:
            replay = await BillingExceptionService().resolve(
                session,
                application_id=application_id,
                exception_id=exception_id,
                resolution="waive",
                reason="idempotent_replay",
            )
        assert waived.status == replay.status == "resolved_waived"
        assert replay.resolution_reason == "customer_service_waiver"
        with pytest.raises(BillingExceptionResolutionConflictError):
            async with sessions() as session:
                await BillingExceptionService().resolve(
                    session,
                    application_id=application_id,
                    exception_id=exception_id,
                    resolution="charge",
                    reason="cannot_rewrite_history",
                )
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_scope_missing_and_already_billed_guards() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id, exception_id, _ = await create_open_exception(sessions)
    try:
        with pytest.raises(BillingExceptionNotFoundError):
            async with sessions() as session:
                await BillingExceptionService().resolve(
                    session,
                    application_id=uuid4(),
                    exception_id=exception_id,
                    resolution="waive",
                    reason="wrong_application",
                )
        with pytest.raises(BillingExceptionNotFoundError):
            async with sessions() as session:
                await BillingExceptionService().resolve(
                    session,
                    application_id=application_id,
                    exception_id=uuid4(),
                    resolution="waive",
                    reason="missing",
                )
        await top_up(sessions, account_id)
        async with sessions() as session:
            await BillingExceptionService().resolve(
                session,
                application_id=application_id,
                exception_id=exception_id,
                resolution="charge",
                reason="charged",
            )
        async with sessions.begin() as session:
            exception = await session.get(BillingException, exception_id)
            assert exception is not None
            exception.status = "open"
            exception.resolution_reason = None
            exception.resolved_at = None
        with pytest.raises(InvalidBillingExceptionError):
            async with sessions() as session:
                await BillingExceptionService().resolve(
                    session,
                    application_id=application_id,
                    exception_id=exception_id,
                    resolution="charge",
                    reason="must_not_duplicate",
                )
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_two_concurrent_charges_and_charge_waive_race() -> None:
    engine, sessions = await database_sessions()
    app1, account1, message1, exception1, _ = await create_open_exception(sessions)
    app2, account2, message2, exception2, _ = await create_open_exception(sessions)

    async def resolve(application_id, exception_id, resolution):
        async with sessions() as session:
            return await BillingExceptionService().resolve(
                session,
                application_id=application_id,
                exception_id=exception_id,
                resolution=resolution,
                reason=f"concurrent_{resolution}",
            )

    try:
        await top_up(sessions, account1)
        charged = await asyncio.gather(
            resolve(app1, exception1, "charge"), resolve(app1, exception1, "charge")
        )
        assert {item.status for item in charged} == {"resolved_charged"}
        _, usage, debits = await counts(sessions, account1, message1)
        assert (usage, debits) == (1, 1)

        await top_up(sessions, account2)
        raced = await asyncio.gather(
            resolve(app2, exception2, "charge"),
            resolve(app2, exception2, "waive"),
            return_exceptions=True,
        )
        assert sum(isinstance(item, BillingExceptionResolutionConflictError) for item in raced) == 1
        terminal = await get_exception(sessions, exception2)
        _, usage, debits = await counts(sessions, account2, message2)
        assert terminal is not None
        if terminal.status == "resolved_charged":
            assert (usage, debits) == (1, 1)
        else:
            assert terminal.status == "resolved_waived"
            assert (usage, debits) == (0, 0)
    finally:
        await cleanup(sessions, app1)
        await cleanup(sessions, app2)
        await engine.dispose()


async def test_exception_charge_races_wallet_reservation_without_overspend() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id, exception_id, _ = await create_open_exception(sessions)
    second_message_id = uuid4()
    try:
        await top_up(sessions, account_id, 60)
        async with sessions() as session:
            external_id = await session.scalar(
                select(BillingAccount.external_id).where(BillingAccount.id == account_id)
            )
        assert external_id is not None
        async with sessions.begin() as session:
            session.add(
                OutboundMessage(
                    id=second_message_id,
                    application_id=application_id,
                    billing_account_id=account_id,
                    channel="whatsapp",
                    recipient="254700000002",
                    message_kind="text",
                    text_body="test-only",
                    provider="meta",
                    status="pending",
                    idempotency_key=f"reservation-race-{uuid4().hex}",
                    payload_hash="1" * 64,
                )
            )

        async def charge():
            async with sessions() as session:
                return await BillingExceptionService().resolve(
                    session,
                    application_id=application_id,
                    exception_id=exception_id,
                    resolution="charge",
                    reason="wallet_replenished",
                )

        async def reserve():
            async with sessions() as session, session.begin():
                quote = await BillingService().quote_and_lock(
                    session,
                    application_id=application_id,
                    external_id=external_id,
                    channel="whatsapp",
                    message_kind="text",
                    billing_category=None,
                )
                return await BillingService().reserve(session, quote, second_message_id)

        results = await asyncio.gather(charge(), reserve(), return_exceptions=True)
        assert (
            sum(
                isinstance(
                    item, (BillingExceptionInsufficientBalanceError, InsufficientBalanceError)
                )
                for item in results
            )
            == 1
        )
        async with sessions() as session:
            account = await session.get(BillingAccount, account_id)
        assert account is not None
        assert account.balance_minor >= 0
        assert account.reserved_minor <= account.balance_minor
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_webhook_replay_races_exception_charge_without_duplicate() -> None:
    engine, sessions = await database_sessions()
    application_id, account_id, message_id, exception_id, provider_id = await create_open_exception(
        sessions
    )
    try:
        await top_up(sessions, account_id)

        async def charge():
            async with sessions() as session:
                return await BillingExceptionService().resolve(
                    session,
                    application_id=application_id,
                    exception_id=exception_id,
                    resolution="charge",
                    reason="wallet_replenished",
                )

        async def webhook():
            async with sessions() as session, session.begin():
                await WhatsAppWebhookService()._reconcile_statuses(
                    session,
                    [{"id": provider_id, "status": "delivered", "timestamp": "1788120001"}],
                )

        await asyncio.gather(charge(), webhook())
        _, usage, debits = await counts(sessions, account_id, message_id)
        assert (usage, debits) == (1, 1)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()
