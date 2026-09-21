import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.whatsapp_outbound_message import OutboundMessage
from app.repositories.whatsapp_messages import WhatsAppMessageRepository
from app.schemas.whatsapp import InboundWhatsAppMessage
from app.services.billing import BillingService
from app.services.reconciliation import ReconciliationService

logger = logging.getLogger(__name__)

STATUS_RANK = {"pending": 0, "uncertain": 0, "sent": 1, "delivered": 2, "read": 3}


def verify_subscription(
    mode: str | None, supplied_token: str | None, expected_token: SecretStr | None
) -> bool:
    return bool(
        mode == "subscribe"
        and supplied_token is not None
        and expected_token is not None
        and hmac.compare_digest(supplied_token, expected_token.get_secret_value())
    )


def verify_signature(
    raw_body: bytes, signature_header: str | None, app_secret: SecretStr | None
) -> bool:
    if signature_header is None or app_secret is None:
        return False
    scheme, separator, supplied_digest = signature_header.partition("=")
    if separator != "=" or scheme != "sha256" or len(supplied_digest) != 64:
        return False
    try:
        bytes.fromhex(supplied_digest)
    except ValueError:
        return False
    expected = hmac.new(
        app_secret.get_secret_value().encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_digest.lower(), expected)


def _values(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return []
    values = []
    for entry in payload.get("entry", []) if isinstance(payload.get("entry"), list) else []:
        for change in (
            entry.get("changes", [])
            if isinstance(entry, dict) and isinstance(entry.get("changes"), list)
            else []
        ):
            value = (
                change.get("value")
                if isinstance(change, dict) and change.get("field") == "messages"
                else None
            )
            if isinstance(value, dict):
                values.append(value)
    return values


def extract_inbound_messages(payload: Any) -> list[InboundWhatsAppMessage]:
    extracted = []
    for value in _values(payload):
        metadata = value.get("metadata")
        recipient_id = metadata.get("phone_number_id") if isinstance(metadata, dict) else None
        messages = value.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            parsed = _parse_message(message, recipient_id, value)
            if parsed is not None:
                extracted.append(parsed)
    return extracted


def _parse_message(
    message: Any, recipient_phone_number_id: Any, event_value: dict[str, Any]
) -> InboundWhatsAppMessage | None:
    if not isinstance(message, dict):
        return None
    message_type = message.get("type")
    text = message.get("text")
    text_body = text.get("body") if message_type == "text" and isinstance(text, dict) else None
    try:
        return InboundWhatsAppMessage(
            meta_message_id=message.get("id"),
            sender_wa_id=message.get("from"),
            recipient_phone_number_id=recipient_phone_number_id,
            message_type=message_type,
            text_body=text_body,
            message_timestamp=datetime.fromtimestamp(int(message.get("timestamp")), tz=UTC),
            raw_payload={"metadata": event_value.get("metadata"), "message": message},
        )
    except (TypeError, ValueError, OSError, ValidationError):
        logger.warning("Ignoring malformed inbound WhatsApp message")
        return None


def extract_status_events(payload: Any) -> list[dict[str, Any]]:
    events = []
    for value in _values(payload):
        statuses = value.get("statuses")
        if not isinstance(statuses, list):
            continue
        for event in statuses:
            if (
                isinstance(event, dict)
                and isinstance(event.get("id"), str)
                and event.get("status") in {"sent", "delivered", "read", "failed"}
            ):
                events.append(event)
    return events


class WhatsAppWebhookService:
    def __init__(
        self,
        repository: WhatsAppMessageRepository | None = None,
        billing_service: BillingService | None = None,
        **_ignored: Any,
    ) -> None:
        self.repository = repository or WhatsAppMessageRepository()
        self.billing_service = billing_service or BillingService()
        self.reconciliation_service = ReconciliationService(self.billing_service)

    async def accept_payload(self, payload: Any, session: AsyncSession) -> tuple[int, int]:
        messages = extract_inbound_messages(payload)
        async with session.begin():
            inserted = (
                await self.repository.insert_inbound_messages(session, messages) if messages else {}
            )
            await self._reconcile_statuses(session, extract_status_events(payload))
        return len(inserted), len(messages) - len(inserted)

    async def _reconcile_statuses(
        self, session: AsyncSession, events: list[dict[str, Any]]
    ) -> None:
        for event in events:
            message = await session.scalar(
                select(OutboundMessage)
                .where(OutboundMessage.provider_message_id == event["id"])
                .with_for_update()
            )
            if message is None:
                logger.info("Ignoring status for unknown WhatsApp provider message")
                continue
            incoming = event["status"]
            if (
                (
                    message.status == "failed"
                    and getattr(message, "reconciliation_status", None) != "released"
                )
                or message.status == "read"
                or (incoming == "failed" and message.status == "delivered")
                or (
                    incoming != "failed"
                    and STATUS_RANK.get(incoming, -1) <= STATUS_RANK.get(message.status, -1)
                )
            ):
                continue
            timestamp = self._timestamp(event.get("timestamp"))
            if (
                message.status == "uncertain"
                and getattr(message, "billing_mode", "customer") == "platform"
            ) or (
                getattr(message, "billing_account_id", None) is not None
                and (
                    message.status == "uncertain"
                    or getattr(message, "reconciliation_status", None) == "released"
                )
            ):
                await self.reconciliation_service.accept_webhook_locked(
                    session,
                    message,
                    provider_message_id=event["id"],
                    timestamp=timestamp,
                    incoming_status=incoming,
                )
            if incoming == "failed":
                message.status = "failed"
                message.failed_at = timestamp
                errors = event.get("errors")
                error = (
                    errors[0]
                    if isinstance(errors, list) and errors and isinstance(errors[0], dict)
                    else {}
                )
                message.error_code = (
                    str(error.get("code"))[:64] if error.get("code") is not None else None
                )
                message.error_message = "WhatsApp delivery failed"
            else:
                message.status = incoming
                setattr(message, f"{incoming}_at", timestamp)
                if getattr(message, "billing_mode", "customer") == "platform":
                    await self.billing_service.record_platform_usage(session, message)
                elif getattr(message, "billing_account_id", None) is not None and getattr(
                    message, "reconciliation_status", None
                ) not in {"released", "billing_exception"}:
                    await self.billing_service.charge(session, message, timestamp)

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (TypeError, ValueError, OSError):
            return datetime.now(UTC)
