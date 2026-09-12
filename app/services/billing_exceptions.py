import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingAccount, BillingReservation, MessageUsage
from app.models.reconciliation import BillingException
from app.models.whatsapp_outbound_message import OutboundMessage
from app.services.billing import BillingService

logger = logging.getLogger(__name__)


class BillingExceptionNotFoundError(Exception):
    pass


class InvalidBillingExceptionError(Exception):
    pass


class BillingExceptionResolutionConflictError(Exception):
    pass


class BillingExceptionInsufficientBalanceError(Exception):
    pass


class BillingExceptionService:
    def __init__(self, billing_service: BillingService | None = None) -> None:
        self.billing_service = billing_service or BillingService()

    async def list(
        self,
        session: AsyncSession,
        *,
        application_id: UUID,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[tuple[BillingException, BillingAccount, OutboundMessage]]:
        statement = (
            select(BillingException, BillingAccount, OutboundMessage)
            .join(BillingAccount, BillingAccount.id == BillingException.billing_account_id)
            .join(OutboundMessage, OutboundMessage.id == BillingException.outbound_message_id)
            .where(BillingAccount.application_id == application_id)
            .order_by(BillingException.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status is not None:
            statement = statement.where(BillingException.status == status)
        return list((await session.execute(statement)).tuples())

    async def get(
        self, session: AsyncSession, *, application_id: UUID, exception_id: UUID
    ) -> tuple[BillingException, BillingAccount, OutboundMessage, BillingReservation]:
        row = (
            await session.execute(
                select(BillingException, BillingAccount, OutboundMessage, BillingReservation)
                .join(BillingAccount, BillingAccount.id == BillingException.billing_account_id)
                .join(OutboundMessage, OutboundMessage.id == BillingException.outbound_message_id)
                .join(
                    BillingReservation,
                    BillingReservation.outbound_message_id == OutboundMessage.id,
                )
                .where(
                    BillingException.id == exception_id,
                    BillingAccount.application_id == application_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise BillingExceptionNotFoundError
        return row

    async def resolve(
        self,
        session: AsyncSession,
        *,
        application_id: UUID,
        exception_id: UUID,
        resolution: str,
        reason: str,
    ) -> BillingException:
        if resolution not in {"charge", "waive"}:
            raise ValueError("Unsupported billing exception resolution")
        reason = reason.strip()
        if not reason or len(reason) > 200:
            raise ValueError("Resolution reason must contain 1 to 200 characters")
        target_status = f"resolved_{'charged' if resolution == 'charge' else 'waived'}"
        now = datetime.now(UTC)
        logger.info(
            "billing_exception_charge_attempted"
            if resolution == "charge"
            else "billing_exception_waive_attempted",
            extra={"billing_exception_id": str(exception_id)},
        )
        async with session.begin():
            # Lock order is outbound -> exception -> reservation -> billing account.
            message_id = await session.scalar(
                select(BillingException.outbound_message_id).where(
                    BillingException.id == exception_id
                )
            )
            if message_id is None:
                raise BillingExceptionNotFoundError
            message = await session.scalar(
                select(OutboundMessage)
                .where(
                    OutboundMessage.id == message_id,
                    OutboundMessage.application_id == application_id,
                )
                .with_for_update()
            )
            if message is None:
                raise BillingExceptionNotFoundError
            exception = await session.scalar(
                select(BillingException)
                .where(
                    BillingException.id == exception_id,
                    BillingException.outbound_message_id == message.id,
                )
                .with_for_update()
            )
            if exception is None:
                raise BillingExceptionNotFoundError
            if exception.status == target_status:
                return exception
            if exception.status != "open":
                raise BillingExceptionResolutionConflictError
            reservation = await session.scalar(
                select(BillingReservation)
                .where(BillingReservation.outbound_message_id == message.id)
                .with_for_update()
            )
            if (
                exception.type != "late_provider_acceptance_after_release"
                or message.provider_message_id is None
                or message.reconciliation_status != "billing_exception"
                or reservation is None
                or reservation.status != "released"
                or reservation.billing_account_id != exception.billing_account_id
                or reservation.amount_minor != exception.amount_minor
                or reservation.currency != exception.currency
            ):
                raise InvalidBillingExceptionError
            usage = await session.scalar(
                select(MessageUsage).where(MessageUsage.outbound_message_id == message.id)
            )
            if usage is not None:
                raise InvalidBillingExceptionError
            if resolution == "charge":
                charged = await self.billing_service.charge(
                    session, message, now, allow_released=True
                )
                if not charged:
                    logger.info(
                        "billing_exception_insufficient_balance",
                        extra={
                            "billing_exception_id": str(exception.id),
                            "billing_account_id": str(exception.billing_account_id),
                        },
                    )
                    raise BillingExceptionInsufficientBalanceError
            exception.status = target_status
            exception.resolution_reason = reason
            exception.resolved_at = now
            logger.info(
                f"billing_exception_{target_status}",
                extra={
                    "billing_exception_id": str(exception.id),
                    "billing_account_id": str(exception.billing_account_id),
                    "outbound_message_id": str(message.id),
                },
            )
            return exception
