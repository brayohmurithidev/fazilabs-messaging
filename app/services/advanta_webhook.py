import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.whatsapp_outbound_message import OutboundMessage
from app.services.billing import BillingService
from app.services.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)

_STATUS = {
    "DeliveredToTerminal": "delivered",
    "SentToNetwork": "sent",
    "AbsentSubscriber": "failed",
    "DeliveryImpossible": "failed",
    "SenderName Blacklisted": "failed",
}


class AdvantaWebhookService:
    def __init__(self, billing_service: BillingService | None = None) -> None:
        self.billing_service = billing_service or BillingService()
        self.reconciliation_service = ReconciliationService(self.billing_service)

    async def accept(self, session: AsyncSession, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        provider_id = payload.get("messageid")
        description = payload.get("description")
        if not isinstance(provider_id, (str, int)) or not isinstance(description, str):
            return
        mapped = _STATUS.get(description)
        if mapped is None:
            return
        async with session.begin():
            message = await session.scalar(
                select(OutboundMessage)
                .where(
                    OutboundMessage.provider == "advanta",
                    OutboundMessage.provider_message_id == str(provider_id),
                )
                .with_for_update()
            )
            if message is None:
                logger.info("Ignoring Advanta status for unknown provider message")
                return
            now = datetime.now(UTC)
            if mapped in {"sent", "delivered"} and message.status == "uncertain":
                await self.reconciliation_service.accept_webhook_locked(
                    session,
                    message,
                    provider_message_id=str(provider_id),
                    timestamp=now,
                    incoming_status=mapped,
                )
            if mapped == "delivered" and message.status not in {"delivered", "failed"}:
                message.status = "delivered"
                message.delivered_at = now
            elif mapped == "sent" and message.status in {"pending", "uncertain"}:
                message.status = "sent"
                message.sent_at = message.sent_at or now
            elif mapped == "failed" and message.status != "failed":
                message.status = "failed"
                message.failed_at = now
                message.error_code = "advanta_delivery_failed"
                message.error_message = "Advanta reported terminal delivery failure"
