from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.whatsapp_outbound_message import OutboundMessage, OutboundMessageStatus


class OutboundMessageRepository:
    async def get_by_idempotency_key(
        self, session: AsyncSession, application_id: UUID, idempotency_key: str
    ) -> OutboundMessage | None:
        return await session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.application_id == application_id,
                OutboundMessage.idempotency_key == idempotency_key,
            )
        )

    async def reserve(self, session: AsyncSession, **values) -> tuple[OutboundMessage, bool]:
        statement = (
            insert(OutboundMessage)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_outbound_app_idempotency")
            .returning(OutboundMessage)
        )
        created = (await session.execute(statement)).scalar_one_or_none()
        if created is not None:
            return created, True
        existing = (
            await session.execute(
                select(OutboundMessage).where(
                    OutboundMessage.application_id == values["application_id"],
                    OutboundMessage.idempotency_key == values["idempotency_key"],
                )
            )
        ).scalar_one()
        return existing, False

    async def get_for_application(
        self, session: AsyncSession, application_id: UUID, message_id: UUID
    ) -> OutboundMessage | None:
        return await session.scalar(
            select(OutboundMessage).where(
                OutboundMessage.id == message_id, OutboundMessage.application_id == application_id
            )
        )

    async def list_for_application(
        self, session: AsyncSession, application_id: UUID, *, filters: list, limit: int, offset: int
    ) -> tuple[list[OutboundMessage], int]:
        conditions = [OutboundMessage.application_id == application_id, *filters]
        query: Select = (
            select(OutboundMessage)
            .where(*conditions)
            .order_by(OutboundMessage.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        items = list((await session.scalars(query)).all())
        total = await session.scalar(
            select(func.count()).select_from(OutboundMessage).where(*conditions)
        )
        return items, int(total or 0)

    async def claim(
        self,
        session: AsyncSession,
        message_id: UUID,
        *,
        claimed_by: str,
        lease_seconds: int,
    ) -> OutboundMessage | None:
        """Atomically transition one message ``pending -> submitting``.

        Succeeds only if the row is still ``pending`` at the moment of the
        UPDATE; a concurrent claimant (another request, or the recovery
        sweeper) can win this race, in which case ``None`` is returned and
        the caller must not contact the provider.
        """
        now = datetime.now(UTC)
        statement = (
            update(OutboundMessage)
            .where(
                OutboundMessage.id == message_id,
                OutboundMessage.status == OutboundMessageStatus.PENDING,
            )
            .values(
                status=OutboundMessageStatus.SUBMITTING,
                claimed_by=claimed_by,
                claimed_at=now,
                claim_lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .returning(OutboundMessage)
        )
        return (await session.execute(statement)).scalar_one_or_none()

    async def claim_abandoned_pending(
        self,
        session: AsyncSession,
        *,
        older_than: datetime,
        claimed_by: str,
        lease_seconds: int,
        limit: int,
    ) -> list[OutboundMessage]:
        """Atomically claim a batch of ``pending`` rows nobody has claimed in time.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` to let concurrent sweeper
        instances safely partition work without blocking each other, then
        claims exactly the rows it locked in one atomic UPDATE.
        """
        now = datetime.now(UTC)
        candidates = (
            select(OutboundMessage.id)
            .where(
                OutboundMessage.status == OutboundMessageStatus.PENDING,
                OutboundMessage.created_at < older_than,
            )
            .order_by(OutboundMessage.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        statement = (
            update(OutboundMessage)
            .where(OutboundMessage.id.in_(candidates))
            .values(
                status=OutboundMessageStatus.SUBMITTING,
                claimed_by=claimed_by,
                claimed_at=now,
                claim_lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            .returning(OutboundMessage)
        )
        return list((await session.execute(statement)).scalars().all())

    async def sweep_expired_submitting(
        self,
        session: AsyncSession,
        *,
        limit: int,
    ) -> list[OutboundMessage]:
        """Move lease-expired ``submitting`` rows to ``uncertain`` only.

        This never calls the provider, never touches billing, and never
        returns a row to ``pending`` — it only records that the outcome of a
        possible prior submission is unknown and requires reconciliation.
        """
        now = datetime.now(UTC)
        candidates = (
            select(OutboundMessage.id)
            .where(
                OutboundMessage.status == OutboundMessageStatus.SUBMITTING,
                OutboundMessage.claim_lease_expires_at < now,
            )
            .order_by(OutboundMessage.claim_lease_expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        statement = (
            update(OutboundMessage)
            .where(OutboundMessage.id.in_(candidates))
            .values(status=OutboundMessageStatus.UNCERTAIN)
            .returning(OutboundMessage)
        )
        return list((await session.execute(statement)).scalars().all())

    async def mark_sent(
        self,
        session: AsyncSession,
        message_id: UUID,
        provider_message_id: str | None,
        timestamp: datetime,
        *,
        expected_status: OutboundMessageStatus = OutboundMessageStatus.SUBMITTING,
    ) -> OutboundMessage | None:
        statement = (
            update(OutboundMessage)
            .where(OutboundMessage.id == message_id, OutboundMessage.status == expected_status)
            .values(
                status=OutboundMessageStatus.SENT,
                provider_message_id=provider_message_id,
                sent_at=timestamp,
                error_code=None,
                error_message=None,
            )
            .returning(OutboundMessage)
        )
        return (await session.execute(statement)).scalar_one_or_none()

    async def mark_error(
        self,
        session: AsyncSession,
        message_id: UUID,
        *,
        status: OutboundMessageStatus,
        error_code: str | None,
        error_message: str,
        timestamp: datetime,
        expected_status: OutboundMessageStatus = OutboundMessageStatus.SUBMITTING,
    ) -> OutboundMessage | None:
        values: dict[str, object] = {
            "status": status,
            "error_code": error_code,
            "error_message": error_message[:1000],
        }
        if status == OutboundMessageStatus.FAILED:
            values["failed_at"] = timestamp
        statement = (
            update(OutboundMessage)
            .where(OutboundMessage.id == message_id, OutboundMessage.status == expected_status)
            .values(**values)
            .returning(OutboundMessage)
        )
        return (await session.execute(statement)).scalar_one_or_none()


WhatsAppOutboundMessageRepository = OutboundMessageRepository
