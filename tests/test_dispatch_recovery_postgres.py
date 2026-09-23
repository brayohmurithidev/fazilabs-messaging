"""Real-PostgreSQL coverage for the M0 durable `submitting` lifecycle.

Covers the claim primitive's atomicity, the recovery sweeper's two
responsibilities (dispatch abandoned `pending`, sweep expired `submitting` to
`uncertain`), the required success-vs-lease-expiry race, and that a
sweeper-originated `uncertain` message is reconciled identically to any other.

All provider interaction uses in-process fakes; no real Advanta/Meta calls.
"""

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
from app.models.reconciliation import MessageReconciliationAttempt
from app.models.whatsapp_outbound_message import OutboundMessage, OutboundMessageStatus
from app.repositories.outbound_messages import OutboundMessageRepository
from app.schemas.whatsapp import WhatsAppSendResult
from app.services.dispatch_sweeper import DispatchSweeper
from app.services.messaging import MessagingService
from app.services.reconciliation import ReconciliationService

pytestmark = pytest.mark.asyncio


class Provider:
    """Fake WhatsApp client. Never calls the real Meta API."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def send_text_message(self, recipient, text):
        self.calls += 1
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return WhatsAppSendResult(
            recipient=recipient, meta_message_id=f"wamid.sweep.{uuid4()}", success=True
        )


class ExplodingProvider:
    """Fails the test if the sweeper ever calls the provider for a row it must not."""

    async def send_text_message(self, recipient, text):
        raise AssertionError("provider must not be called for an expired `submitting` row")


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


async def seed_application(sessions, *, billing_required: bool = False):
    application_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            MessagingApplication(
                id=application_id,
                name="Dispatch recovery test",
                slug=f"dispatch-recovery-{uuid4().hex}",
                billing_required=billing_required,
            )
        )
    return application_id


async def seed_billing(sessions, application_id, *, balance=100, amount=60):
    account_id = uuid4()
    rule_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            BillingAccount(
                id=account_id,
                application_id=application_id,
                external_id=f"school-{uuid4().hex}",
                name="Dispatch Test School",
                currency="KES",
                balance_minor=balance,
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
    return account_id, rule_id


async def seed_message(
    sessions,
    application_id,
    *,
    status: str,
    created_at: datetime,
    billing_account_id=None,
    billing_mode: str = "customer",
    claim_lease_expires_at: datetime | None = None,
    claimed_by: str | None = None,
    idempotency_key: str | None = None,
) -> OutboundMessage:
    message_id = uuid4()
    async with sessions.begin() as session:
        session.add(
            OutboundMessage(
                id=message_id,
                application_id=application_id,
                billing_account_id=billing_account_id,
                channel="whatsapp",
                recipient="254700000001",
                message_kind="text",
                text_body="dispatch recovery test message",
                provider="meta",
                billing_mode=billing_mode,
                status=status,
                idempotency_key=idempotency_key or f"dispatch-{uuid4().hex}",
                payload_hash="0" * 64,
                created_at=created_at,
                claim_lease_expires_at=claim_lease_expires_at,
                claimed_by=claimed_by,
            )
        )
    return message_id


async def seed_reservation(sessions, *, billing_account_id, message_id, rule_id, amount=60):
    async with sessions.begin() as session:
        account = await session.get(BillingAccount, billing_account_id, with_for_update=True)
        account.reserved_minor += amount
        session.add(
            BillingReservation(
                billing_account_id=billing_account_id,
                outbound_message_id=message_id,
                pricing_rule_id=rule_id,
                amount_minor=amount,
                currency="KES",
            )
        )


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


async def load(sessions, message_id) -> OutboundMessage:
    async with sessions() as session:
        return await session.get(OutboundMessage, message_id)


async def reservation_status(sessions, message_id) -> str | None:
    async with sessions() as session:
        reservation = await session.scalar(
            select(BillingReservation).where(BillingReservation.outbound_message_id == message_id)
        )
        return reservation.status if reservation else None


def make_sweeper(provider, *, pending_grace_seconds=60, claim_lease_seconds=90, batch_size=25):
    messaging_service = MessagingService(
        provider, claim_lease_seconds=claim_lease_seconds, claim_identity="sweeper:test"
    )
    return DispatchSweeper(
        messaging_service,
        sweeper_id="sweeper:test",
        pending_grace_seconds=pending_grace_seconds,
        claim_lease_seconds=claim_lease_seconds,
        batch_size=batch_size,
    )


# ---------------------------------------------------------------------------
# Claim primitive atomicity
# ---------------------------------------------------------------------------


async def test_pending_can_be_claimed_exactly_once_sequentially() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    message_id = await seed_message(
        sessions, application_id, status="pending", created_at=datetime.now(UTC)
    )
    try:
        repository = OutboundMessageRepository()
        async with sessions() as session:
            first = await repository.claim(
                session, message_id, claimed_by="worker-a", lease_seconds=90
            )
            await session.commit()
        assert first is not None
        assert first.status == OutboundMessageStatus.SUBMITTING
        async with sessions() as session:
            second = await repository.claim(
                session, message_id, claimed_by="worker-b", lease_seconds=90
            )
            await session.commit()
        assert second is None
        message = await load(sessions, message_id)
        assert message.status == "submitting"
        assert message.claimed_by == "worker-a"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_two_concurrent_claimers_exactly_one_wins() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    message_id = await seed_message(
        sessions, application_id, status="pending", created_at=datetime.now(UTC)
    )
    repository = OutboundMessageRepository()

    async def claim(identity: str):
        async with sessions() as session:
            result = await repository.claim(
                session, message_id, claimed_by=identity, lease_seconds=90
            )
            await session.commit()
            return result

    try:
        results = await asyncio.gather(claim("worker-a"), claim("worker-b"))
        winners = [result for result in results if result is not None]
        assert len(winners) == 1
        message = await load(sessions, message_id)
        assert message.status == "submitting"
        assert message.claimed_by in {"worker-a", "worker-b"}
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_two_sweepers_partition_abandoned_pending_without_overlap() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    old = datetime.now(UTC) - timedelta(seconds=120)
    message_ids = [
        await seed_message(sessions, application_id, status="pending", created_at=old)
        for _ in range(6)
    ]
    repository = OutboundMessageRepository()

    async def claim_batch(identity: str):
        async with sessions() as session:
            claimed = await repository.claim_abandoned_pending(
                session,
                older_than=datetime.now(UTC) - timedelta(seconds=60),
                claimed_by=identity,
                lease_seconds=90,
                limit=25,
            )
            await session.commit()
            return claimed

    try:
        first_batch, second_batch = await asyncio.gather(
            claim_batch("sweeper-a"), claim_batch("sweeper-b")
        )
        claimed_ids = [message.id for message in [*first_batch, *second_batch]]
        assert sorted(claimed_ids) == sorted(message_ids)
        assert len(claimed_ids) == len(set(claimed_ids))
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_live_request_claim_races_sweeper_claim_exactly_one_wins() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    old = datetime.now(UTC) - timedelta(seconds=120)
    message_id = await seed_message(sessions, application_id, status="pending", created_at=old)
    repository = OutboundMessageRepository()

    async def request_claim():
        async with sessions() as session:
            result = await repository.claim(
                session, message_id, claimed_by="live-request", lease_seconds=90
            )
            await session.commit()
            return [result] if result is not None else []

    async def sweeper_claim():
        async with sessions() as session:
            result = await repository.claim_abandoned_pending(
                session,
                older_than=datetime.now(UTC) - timedelta(seconds=60),
                claimed_by="sweeper",
                lease_seconds=90,
                limit=25,
            )
            await session.commit()
            return result

    try:
        request_result, sweeper_result = await asyncio.gather(request_claim(), sweeper_claim())
        total_claimed = len(request_result) + len(sweeper_result)
        assert total_claimed == 1
        message = await load(sessions, message_id)
        assert message.status == "submitting"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# The required race: late synchronous success vs. sweeper lease expiry
# ---------------------------------------------------------------------------


async def test_success_completion_vs_lease_expiry_never_double_charges_or_overwrites() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    account_id, rule_id = await seed_billing(sessions, application_id)
    expired_lease = datetime.now(UTC) - timedelta(seconds=1)
    message_id = await seed_message(
        sessions,
        application_id,
        status="submitting",
        created_at=datetime.now(UTC) - timedelta(seconds=200),
        billing_account_id=account_id,
        claim_lease_expires_at=expired_lease,
        claimed_by="live-request",
    )
    await seed_reservation(
        sessions, billing_account_id=account_id, message_id=message_id, rule_id=rule_id
    )
    repository = OutboundMessageRepository()
    provider = Provider()
    messaging_service = MessagingService(provider)

    async def late_success():
        async with sessions() as session:
            async with session.begin():
                message = await session.get(OutboundMessage, message_id)
            await messaging_service._record_provider_success(
                session, message, "wamid.late-success", datetime.now(UTC)
            )

    async def sweep_expiry():
        async with sessions() as session:
            await repository.sweep_expired_submitting(session, limit=25)
            await session.commit()

    try:
        await asyncio.gather(late_success(), sweep_expiry())
        message = await load(sessions, message_id)
        async with sessions() as session:
            account = await session.get(BillingAccount, account_id)
            usage_count = await session.scalar(
                select(func.count())
                .select_from(MessageUsage)
                .where(MessageUsage.outbound_message_id == message_id)
            )
            debit_count = await session.scalar(
                select(func.count())
                .select_from(WalletTransaction)
                .where(
                    WalletTransaction.billing_account_id == account_id,
                    WalletTransaction.type == "debit",
                )
            )
        # Exactly one of the two guarded outcomes may have won. Both are
        # individually valid; what must NEVER happen is a mix of the two
        # (e.g. sent-but-uncharged, or uncertain-but-charged, or a double
        # charge, or a released reservation).
        if message.status == "sent":
            assert (account.balance_minor, account.reserved_minor) == (40, 0)
            assert (usage_count, debit_count) == (1, 1)
        else:
            assert message.status == "uncertain"
            assert (account.balance_minor, account.reserved_minor) == (100, 60)
            assert (usage_count, debit_count) == (0, 0)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# Sweeper responsibility A: abandoned pending
# ---------------------------------------------------------------------------


async def test_sweeper_dispatches_abandoned_pending_exactly_once() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    old = datetime.now(UTC) - timedelta(seconds=200)
    message_id = await seed_message(sessions, application_id, status="pending", created_at=old)
    provider = Provider()
    sweeper = make_sweeper(provider, pending_grace_seconds=60)
    try:
        async with sessions() as session:
            result = await sweeper.sweep_once(session)
        assert (result.dispatched_from_pending, result.expired_submitting_to_uncertain) == (1, 0)
        assert provider.calls == 1
        message = await load(sessions, message_id)
        assert message.status == "sent"
        assert message.provider_message_id is not None
        assert message.claimed_by == "sweeper:test"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_sweeper_ignores_pending_row_younger_than_grace_period() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    message_id = await seed_message(
        sessions, application_id, status="pending", created_at=datetime.now(UTC)
    )
    provider = Provider()
    sweeper = make_sweeper(provider, pending_grace_seconds=60)
    try:
        async with sessions() as session:
            result = await sweeper.sweep_once(session)
        assert result.dispatched_from_pending == 0
        assert provider.calls == 0
        message = await load(sessions, message_id)
        assert message.status == "pending"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_sweeper_recovers_platform_funded_message_without_touching_wallet() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    old = datetime.now(UTC) - timedelta(seconds=200)
    message_id = await seed_message(
        sessions, application_id, status="pending", created_at=old, billing_mode="platform"
    )
    provider = Provider()
    sweeper = make_sweeper(provider, pending_grace_seconds=60)
    try:
        async with sessions() as session:
            await sweeper.sweep_once(session)
        message = await load(sessions, message_id)
        assert message.status == "sent"
        async with sessions() as session:
            usage = await session.scalar(
                select(MessageUsage).where(MessageUsage.outbound_message_id == message_id)
            )
        assert usage is not None
        assert usage.billing_mode == "platform"
        assert usage.customer_price_minor == 0
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# Sweeper responsibility B: expired submitting -> uncertain only
# ---------------------------------------------------------------------------


async def test_sweeper_moves_expired_submitting_to_uncertain_without_side_effects() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    account_id, rule_id = await seed_billing(sessions, application_id)
    message_id = await seed_message(
        sessions,
        application_id,
        status="submitting",
        created_at=datetime.now(UTC) - timedelta(seconds=200),
        billing_account_id=account_id,
        claim_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        claimed_by="dead-worker",
    )
    await seed_reservation(
        sessions, billing_account_id=account_id, message_id=message_id, rule_id=rule_id
    )
    sweeper = make_sweeper(ExplodingProvider())
    try:
        async with sessions() as session:
            result = await sweeper.sweep_once(session)
        assert (result.dispatched_from_pending, result.expired_submitting_to_uncertain) == (0, 1)
        message = await load(sessions, message_id)
        assert message.status == "uncertain"
        assert await reservation_status(sessions, message_id) == "active"
        async with sessions() as session:
            account = await session.get(BillingAccount, account_id)
        assert (account.balance_minor, account.reserved_minor) == (100, 60)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_sweeper_ignores_submitting_row_with_unexpired_lease() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    message_id = await seed_message(
        sessions,
        application_id,
        status="submitting",
        created_at=datetime.now(UTC),
        claim_lease_expires_at=datetime.now(UTC) + timedelta(seconds=90),
        claimed_by="live-worker",
    )
    sweeper = make_sweeper(ExplodingProvider())
    try:
        async with sessions() as session:
            result = await sweeper.sweep_once(session)
        assert result.expired_submitting_to_uncertain == 0
        message = await load(sessions, message_id)
        assert message.status == "submitting"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_sweeper_ignores_terminal_rows() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    old = datetime.now(UTC) - timedelta(seconds=200)
    sent_id = await seed_message(sessions, application_id, status="sent", created_at=old)
    failed_id = await seed_message(sessions, application_id, status="failed", created_at=old)
    uncertain_id = await seed_message(sessions, application_id, status="uncertain", created_at=old)
    sweeper = make_sweeper(ExplodingProvider())
    try:
        async with sessions() as session:
            result = await sweeper.sweep_once(session)
        assert (result.dispatched_from_pending, result.expired_submitting_to_uncertain) == (0, 0)
        for message_id, expected in (
            (sent_id, "sent"),
            (failed_id, "failed"),
            (uncertain_id, "uncertain"),
        ):
            message = await load(sessions, message_id)
            assert message.status == expected
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# Reconciliation of a sweeper-originated `uncertain` row
# ---------------------------------------------------------------------------


async def test_sweep_originated_uncertain_row_reconciles_accepted_charges_once() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    account_id, rule_id = await seed_billing(sessions, application_id)
    message_id = await seed_message(
        sessions,
        application_id,
        status="submitting",
        created_at=datetime.now(UTC) - timedelta(seconds=200),
        billing_account_id=account_id,
        claim_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        claimed_by="dead-worker",
    )
    await seed_reservation(
        sessions, billing_account_id=account_id, message_id=message_id, rule_id=rule_id
    )
    sweeper = make_sweeper(ExplodingProvider())
    try:
        async with sessions() as session:
            await sweeper.sweep_once(session)
        assert (await load(sessions, message_id)).status == "uncertain"

        async with sessions() as session:
            await ReconciliationService().reconcile(
                session,
                application_id=application_id,
                message_id=message_id,
                outcome="accepted",
                reason="operator_confirmed_via_meta_business_manager",
                provider_message_id="wamid.recovered-after-sweep",
            )
        message = await load(sessions, message_id)
        assert message.status == "sent"
        async with sessions() as session:
            account = await session.get(BillingAccount, account_id)
            usage_count = await session.scalar(
                select(func.count())
                .select_from(MessageUsage)
                .where(MessageUsage.outbound_message_id == message_id)
            )
            debit_count = await session.scalar(
                select(func.count())
                .select_from(WalletTransaction)
                .where(
                    WalletTransaction.billing_account_id == account_id,
                    WalletTransaction.type == "debit",
                )
            )
        assert (account.balance_minor, account.reserved_minor) == (40, 0)
        assert (usage_count, debit_count) == (1, 1)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_sweep_originated_uncertain_row_reconciles_rejected_releases_once() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    account_id, rule_id = await seed_billing(sessions, application_id)
    message_id = await seed_message(
        sessions,
        application_id,
        status="submitting",
        created_at=datetime.now(UTC) - timedelta(seconds=200),
        billing_account_id=account_id,
        claim_lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
        claimed_by="dead-worker",
    )
    await seed_reservation(
        sessions, billing_account_id=account_id, message_id=message_id, rule_id=rule_id
    )
    sweeper = make_sweeper(ExplodingProvider())
    try:
        async with sessions() as session:
            await sweeper.sweep_once(session)

        async with sessions() as session:
            await ReconciliationService().reconcile(
                session,
                application_id=application_id,
                message_id=message_id,
                outcome="rejected",
                reason="operator_confirmed_non_delivery",
            )
        message = await load(sessions, message_id)
        assert message.status == "failed"
        assert await reservation_status(sessions, message_id) == "released"
        async with sessions() as session:
            account = await session.get(BillingAccount, account_id)
        assert (account.balance_minor, account.reserved_minor) == (100, 0)
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# Idempotency under concurrency (section 28-F)
# ---------------------------------------------------------------------------


async def test_concurrent_duplicate_idempotent_requests_produce_one_message_one_provider_call() -> (
    None
):
    from app.schemas.messages import TextMessageRequest

    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    async with sessions() as session:
        application = await session.get(MessagingApplication, application_id)
    provider = Provider()
    request = TextMessageRequest(channel="whatsapp", to="254700000001", text="idempotent race")

    async def send():
        async with sessions() as session:
            return await MessagingService(provider).send_text(
                session, application, "dispatch-idempotent-race-001", request
            )

    try:
        results = await asyncio.gather(send(), send())
        assert results[0].id == results[1].id
        assert provider.calls == 1
        async with sessions() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(OutboundMessage)
                .where(OutboundMessage.application_id == application_id)
            )
        assert count == 1
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


# ---------------------------------------------------------------------------
# M0.1: a lease never expires before its provider attempt has started
# ---------------------------------------------------------------------------


async def seed_tagged_pending(sessions, application_id, count: int) -> list:
    """Abandoned pending rows whose text body identifies them to a fake provider."""
    old = datetime.now(UTC) - timedelta(seconds=200)
    ids = [
        await seed_message(sessions, application_id, status="pending", created_at=old)
        for _ in range(count)
    ]
    async with sessions.begin() as session:
        for message_id in ids:
            message = await session.get(OutboundMessage, message_id)
            message.text_body = f"tag:{message_id}"
    return ids


class InvariantCheckingProvider:
    """Slow fake provider that records, at the instant of every provider
    call, whether the row being submitted still held a live claim."""

    def __init__(self, sessions, *, delay: float, fail_on_call: int | None = None) -> None:
        self.sessions = sessions
        self.delay = delay
        self.fail_on_call = fail_on_call
        self.calls: list = []
        self.violations: list[str] = []

    async def send_text_message(self, recipient, text):
        message_id = text.removeprefix("tag:")
        self.calls.append(message_id)
        async with self.sessions() as session:
            row = await session.get(OutboundMessage, message_id)
        if row.status != "submitting" or row.claim_lease_expires_at <= datetime.now(UTC):
            self.violations.append(f"{message_id}:{row.status}")
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("simulated sweeper process crash mid-provider-call")
        await asyncio.sleep(self.delay)
        return WhatsAppSendResult(
            recipient=recipient, meta_message_id=f"wamid.{message_id}", success=True
        )


async def test_slow_batch_with_overlapping_sweeper_never_attempts_an_expired_claim() -> None:
    """Regression for the batch-wide lease: 5 rows x 0.4s sequential provider
    time outlives a 1s lease. Under the old pre-claimed batch, the
    overlapping sweeper moved not-yet-attempted rows to `uncertain` and the
    first sweeper then submitted them anyway."""
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    ids = await seed_tagged_pending(sessions, application_id, 5)
    provider = InvariantCheckingProvider(sessions, delay=0.4)
    first = make_sweeper(provider, claim_lease_seconds=1)
    expirer = make_sweeper(ExplodingProvider(), claim_lease_seconds=1)
    done = asyncio.Event()

    async def run_first():
        try:
            async with sessions() as session:
                return await first.sweep_once(session)
        finally:
            done.set()

    async def overlapping_expiry_sweeps():
        expired = 0
        while not done.is_set():
            async with sessions() as session:
                expired += await expirer._sweep_expired_submitting(session)
            await asyncio.sleep(0.05)
        return expired

    try:
        result, expired = await asyncio.gather(run_first(), overlapping_expiry_sweeps())
        assert provider.violations == []
        assert expired == 0
        assert sorted(provider.calls) == sorted(str(i) for i in ids)
        assert result.dispatched_from_pending == 5
        assert result.dispatch_outcomes == {"sent": 5}
        for message_id in ids:
            message = await load(sessions, message_id)
            assert message.status == "sent"
            assert message.provider_message_id == f"wamid.{message_id}"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_two_full_sweepers_overlap_without_double_or_expired_submission() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    ids = await seed_tagged_pending(sessions, application_id, 6)
    provider = InvariantCheckingProvider(sessions, delay=0.2)
    sweeper_a = make_sweeper(provider, claim_lease_seconds=1)
    sweeper_b = make_sweeper(provider, claim_lease_seconds=1)
    sweeper_b.sweeper_id = "sweeper:test-b"
    sweeper_b.messaging_service.claim_identity = "sweeper:test-b"

    async def sweep(sweeper):
        async with sessions() as session:
            return await sweeper.sweep_once(session)

    try:
        result_a, result_b = await asyncio.gather(sweep(sweeper_a), sweep(sweeper_b))
        assert provider.violations == []
        assert sorted(provider.calls) == sorted(str(i) for i in ids)
        assert result_a.dispatched_from_pending + result_b.dispatched_from_pending == 6
        for message_id in ids:
            assert (await load(sessions, message_id)).status == "sent"
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


@pytest.mark.parametrize("race", ["lease_already_expired", "expired_to_uncertain_by_other_sweeper"])
async def test_dispatch_refuses_to_start_without_a_live_claim(race) -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    lease = -1 if race == "lease_already_expired" else 60
    message_id = await seed_message(
        sessions,
        application_id,
        status="submitting",
        created_at=datetime.now(UTC) - timedelta(seconds=200),
        claim_lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease),
        claimed_by="sweeper:test",
    )
    service = MessagingService(
        ExplodingProvider(), claim_identity="sweeper:test", claim_lease_seconds=90
    )
    try:
        async with sessions() as session:
            async with session.begin():
                stale_view = await session.get(OutboundMessage, message_id)
            if race == "expired_to_uncertain_by_other_sweeper":
                async with sessions.begin() as other:
                    row = await other.get(OutboundMessage, message_id)
                    row.status = OutboundMessageStatus.UNCERTAIN
            result = await service.dispatch_claimed_from_row(session, stale_view)
        expected = "submitting" if race == "lease_already_expired" else "uncertain"
        assert result.status == expected
        assert (await load(sessions, message_id)).status == expected
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_crash_mid_sweep_leaves_only_the_in_flight_row_submitting() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    ids = await seed_tagged_pending(sessions, application_id, 4)
    provider = InvariantCheckingProvider(sessions, delay=0, fail_on_call=2)
    sweeper = make_sweeper(provider)
    try:
        with pytest.raises(RuntimeError, match="simulated sweeper process crash"):
            async with sessions() as session:
                await sweeper.sweep_once(session)
        statuses = sorted([(await load(sessions, i)).status for i in ids])
        # One sent, the in-flight one durably `submitting` (never resent;
        # expires to `uncertain`), the rest never claimed.
        assert statuses == ["pending", "pending", "sent", "submitting"]
        assert len(provider.calls) == 2
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()


async def test_backlog_larger_than_batch_drains_over_successive_sweeps() -> None:
    engine, sessions = await database_sessions()
    application_id = await seed_application(sessions)
    ids = await seed_tagged_pending(sessions, application_id, 7)
    provider = InvariantCheckingProvider(sessions, delay=0)
    sweeper = make_sweeper(provider, batch_size=3)
    try:
        counts = []
        for _ in range(3):
            async with sessions() as session:
                counts.append((await sweeper.sweep_once(session)).dispatched_from_pending)
        assert counts == [3, 3, 1]
        assert len(provider.calls) == 7
        assert {(await load(sessions, i)).status for i in ids} == {"sent"}
    finally:
        await cleanup(sessions, application_id)
        await engine.dispose()
