import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import BillingAccount, BillingReservation
from app.models.reconciliation import BillingException, MessageReconciliationAttempt
from app.models.whatsapp_outbound_message import OutboundMessage
from app.services.billing import BillingService

logger = logging.getLogger(__name__)


class ReconciliationNotFoundError(Exception):
    pass


class InvalidReconciliationTransitionError(Exception):
    pass


class ReconciliationTooRecentError(Exception):
    pass


class ProviderMessageIdRequiredError(Exception):
    pass


class ReconciliationService:
    def __init__(self, billing_service: BillingService | None = None) -> None:
        self.billing_service = billing_service or BillingService()

    async def list_stale(
        self,
        session: AsyncSession,
        application_id: UUID,
        *,
        older_than: datetime,
        limit: int,
        offset: int,
    ) -> list[tuple[OutboundMessage, BillingReservation, BillingAccount]]:
        return list(
            (
                await session.execute(
                    select(OutboundMessage, BillingReservation, BillingAccount)
                    .join(
                        BillingReservation,
                        BillingReservation.outbound_message_id == OutboundMessage.id,
                    )
                    .join(
                        BillingAccount, BillingAccount.id == BillingReservation.billing_account_id
                    )
                    .where(
                        OutboundMessage.application_id == application_id,
                        OutboundMessage.status == "uncertain",
                        BillingReservation.status == "active",
                        BillingReservation.created_at <= older_than,
                    )
                    .order_by(BillingReservation.created_at)
                    .limit(limit)
                    .offset(offset)
                )
            ).tuples()
        )

    async def reconcile(
        self,
        session: AsyncSession,
        *,
        application_id: UUID,
        message_id: UUID,
        outcome: str,
        reason: str,
        provider_message_id: str | None = None,
    ) -> OutboundMessage:
        now = datetime.now(UTC)
        async with session.begin():
            message = await self._lock_message(session, application_id, message_id)
            if outcome == "unknown":
                if message.status != "uncertain":
                    raise InvalidReconciliationTransitionError
                self._audit(session, message.id, "unresolved", reason, provider_message_id, now)
                logger.info(
                    "reconciliation_attempted", extra={"outbound_message_id": str(message.id)}
                )
                return message
            if outcome == "accepted":
                if not provider_message_id:
                    raise ProviderMessageIdRequiredError
                await self._accept_locked(
                    session,
                    message,
                    provider_message_id=provider_message_id,
                    timestamp=now,
                    method="operator",
                    reason=reason,
                )
                return message
            if outcome == "rejected":
                if message.status != "uncertain":
                    if message.reconciliation_status == "resolved_rejected":
                        return message
                    raise InvalidReconciliationTransitionError
                await self.billing_service.release(session, message.id)
                message.status = "failed"
                message.failed_at = now
                message.error_code = "provider_non_acceptance_confirmed"
                message.error_message = "Provider non-acceptance confirmed by reconciliation"
                message.reconciliation_status = "resolved_rejected"
                self._audit(session, message.id, "resolved_rejected", reason, None, now)
                logger.info(
                    "reconciliation_resolved_rejected",
                    extra={"outbound_message_id": str(message.id)},
                )
                return message
            raise ValueError("Unsupported reconciliation outcome")

    async def release_stale(
        self,
        session: AsyncSession,
        *,
        application_id: UUID,
        message_id: UUID,
        reason: str,
        threshold_minutes: int,
        force: bool,
    ) -> OutboundMessage:
        now = datetime.now(UTC)
        async with session.begin():
            message = await self._lock_message(session, application_id, message_id)
            reservation = await self._lock_reservation(session, message.id)
            if message.reconciliation_status == "released" and reservation.status == "released":
                return message
            if message.status != "uncertain" or reservation.status != "active":
                raise InvalidReconciliationTransitionError
            if not force and reservation.created_at > now - timedelta(minutes=threshold_minutes):
                raise ReconciliationTooRecentError
            await self.billing_service.release(session, message.id)
            message.status = "failed"
            message.failed_at = now
            message.error_code = "operator_abandoned_uncertain"
            message.error_message = "Uncertain message abandoned after operator review"
            message.reconciliation_status = "released"
            self._audit(session, message.id, "released", reason, message.provider_message_id, now)
            logger.info(
                "uncertain_reservation_released",
                extra={"outbound_message_id": str(message.id)},
            )
            return message

    async def accept_webhook_locked(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        *,
        provider_message_id: str,
        timestamp: datetime,
        incoming_status: str,
    ) -> None:
        await self._accept_locked(
            session,
            message,
            provider_message_id=provider_message_id,
            timestamp=timestamp,
            method="webhook",
            reason=f"provider_{incoming_status}_callback",
        )

    async def _accept_locked(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        *,
        provider_message_id: str,
        timestamp: datetime,
        method: str,
        reason: str,
    ) -> None:
        if message.reconciliation_status in {"resolved_accepted", "billing_exception"}:
            return
        if message.status in {"delivered", "read"} and message.reconciliation_status != "released":
            return
        if message.status not in {"uncertain", "failed", "sent", "delivered", "read"}:
            raise InvalidReconciliationTransitionError
        if message.status == "failed" and message.reconciliation_status != "released":
            raise InvalidReconciliationTransitionError
        reservation = await self._lock_reservation(session, message.id)
        message.provider_message_id = provider_message_id
        message.status = "sent"
        message.sent_at = timestamp
        charged = await self.billing_service.charge(
            session, message, timestamp, allow_released=reservation.status == "released"
        )
        if charged:
            message.reconciliation_status = "resolved_accepted"
            self._audit(
                session,
                message.id,
                "resolved_accepted",
                reason,
                provider_message_id,
                timestamp,
                method,
            )
            logger.info(
                "reconciliation_resolved_accepted",
                extra={"outbound_message_id": str(message.id)},
            )
            return
        message.reconciliation_status = "billing_exception"
        session.add(
            BillingException(
                billing_account_id=reservation.billing_account_id,
                outbound_message_id=message.id,
                type="late_provider_acceptance_after_release",
                amount_minor=reservation.amount_minor,
                currency=reservation.currency,
                reason="Late provider acceptance confirmed after reservation release",
            )
        )
        logger.warning(
            "billing_exception_opened",
            extra={
                "billing_account_id": str(reservation.billing_account_id),
                "outbound_message_id": str(message.id),
                "exception_type": "late_provider_acceptance_after_release",
            },
        )
        self._audit(
            session, message.id, "resolved_accepted", reason, provider_message_id, timestamp, method
        )
        logger.warning("late_provider_acceptance", extra={"outbound_message_id": str(message.id)})
        logger.warning(
            "billing_reconciliation_exception",
            extra={"outbound_message_id": str(message.id)},
        )

    async def _lock_message(
        self, session: AsyncSession, application_id: UUID, message_id: UUID
    ) -> OutboundMessage:
        message = await session.scalar(
            select(OutboundMessage)
            .where(
                OutboundMessage.id == message_id,
                OutboundMessage.application_id == application_id,
            )
            .with_for_update()
        )
        if message is None:
            raise ReconciliationNotFoundError
        return message

    async def _lock_reservation(
        self, session: AsyncSession, message_id: UUID
    ) -> BillingReservation:
        reservation = await session.scalar(
            select(BillingReservation)
            .where(BillingReservation.outbound_message_id == message_id)
            .with_for_update()
        )
        if reservation is None:
            raise InvalidReconciliationTransitionError
        return reservation

    @staticmethod
    def _audit(
        session: AsyncSession,
        message_id: UUID,
        status: str,
        reason: str,
        provider_message_id: str | None,
        timestamp: datetime,
        method: str = "operator",
    ) -> None:
        session.add(
            MessageReconciliationAttempt(
                outbound_message_id=message_id,
                status=status,
                method=method,
                reason_code=reason[:64],
                notes=None,
                provider_message_id=provider_message_id,
                started_at=timestamp,
                completed_at=timestamp,
            )
        )
