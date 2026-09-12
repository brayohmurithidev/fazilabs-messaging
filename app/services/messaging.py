import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.messaging_application import MessagingApplication
from app.models.whatsapp_outbound_message import OutboundMessage, OutboundMessageStatus
from app.repositories.message_templates import MessageTemplateRepository
from app.repositories.outbound_messages import OutboundMessageRepository
from app.schemas.messages import TemplateMessageRequest, TextMessageRequest
from app.services.billing import BillingQuote, BillingService
from app.services.template_parameters import (
    InvalidTemplateParametersError,
    InvalidTemplateSchemaError,
    ordered_body_values,
)
from app.services.whatsapp_client import WhatsAppAPIError, WhatsAppCloudAPIClient


class IdempotencyConflictError(Exception):
    pass


class BillingAccountRequiredError(Exception):
    pass


class TemplateNotFoundError(Exception):
    pass


class TemplateUnavailableError(Exception):
    pass


class TemplateParameterError(Exception):
    pass


def canonical_payload_hash(payload: TextMessageRequest | TemplateMessageRequest) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class MessagingService:
    def __init__(
        self,
        client: WhatsAppCloudAPIClient,
        repository: OutboundMessageRepository | None = None,
        template_repository: MessageTemplateRepository | None = None,
        billing_service: BillingService | None = None,
    ) -> None:
        self.client = client
        self.repository = repository or OutboundMessageRepository()
        self.template_repository = template_repository or MessageTemplateRepository()
        self.billing_service = billing_service or BillingService()

    async def send_text(
        self,
        session: AsyncSession,
        application: MessagingApplication,
        idempotency_key: str,
        request: TextMessageRequest,
    ) -> OutboundMessage:
        payload_hash = canonical_payload_hash(request)
        metadata = request.metadata or {}
        quote: BillingQuote | None = None
        async with session.begin():
            existing = await self.repository.get_by_idempotency_key(
                session, application.id, idempotency_key
            )
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise IdempotencyConflictError
                return existing
            if getattr(application, "billing_required", False) and request.billing_account is None:
                raise BillingAccountRequiredError
            if request.billing_account is not None:
                quote = await self.billing_service.quote_and_lock(
                    session,
                    application_id=application.id,
                    external_id=request.billing_account,
                    channel=request.channel.value,
                    message_kind="text",
                    billing_category=None,
                )
            message, created = await self.repository.reserve(
                session,
                application_id=application.id,
                billing_account_id=quote.account.id if quote else None,
                channel=request.channel.value,
                recipient=request.to,
                message_kind="text",
                billing_category=None,
                text_body=request.text,
                provider="meta",
                status=OutboundMessageStatus.PENDING,
                source_type=metadata.get("source_type"),
                source_id=metadata.get("source_id"),
                message_metadata=request.metadata,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if created and quote is not None:
                await self.billing_service.reserve(session, quote, message.id)
        if not created:
            if message.payload_hash != payload_hash:
                raise IdempotencyConflictError
            return message
        now = datetime.now(UTC)
        try:
            result = await self.client.send_text_message(request.to, request.text)
        except WhatsAppAPIError as exc:
            failure_status = (
                OutboundMessageStatus.UNCERTAIN
                if exc.ambiguous_delivery
                else OutboundMessageStatus.FAILED
            )
            async with session.begin():
                await self.repository.mark_error(
                    session,
                    message.id,
                    status=failure_status,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    timestamp=now,
                )
                if not exc.ambiguous_delivery and getattr(message, "billing_account_id", None):
                    await self.billing_service.release(session, message.id)
            await session.refresh(message)
            return message
        if result.meta_message_id is None:
            async with session.begin():
                await self.repository.mark_error(
                    session,
                    message.id,
                    status=OutboundMessageStatus.UNCERTAIN,
                    error_code=None,
                    error_message="Provider success response omitted message ID",
                    timestamp=now,
                )
            await session.refresh(message)
            return message
        async with session.begin():
            await self.repository.mark_sent(session, message.id, result.meta_message_id, now)
            if getattr(message, "billing_account_id", None):
                await self.billing_service.charge(session, message, now)
        await session.refresh(message)
        return message

    async def send_template(
        self,
        session: AsyncSession,
        application: MessagingApplication,
        idempotency_key: str,
        request: TemplateMessageRequest,
    ) -> OutboundMessage:
        payload_hash = canonical_payload_hash(request)
        metadata = request.metadata or {}
        quote: BillingQuote | None = None
        async with session.begin():
            existing = await self.repository.get_by_idempotency_key(
                session, application.id, idempotency_key
            )
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise IdempotencyConflictError
                return existing
            if getattr(application, "billing_required", False) and request.billing_account is None:
                raise BillingAccountRequiredError
            template = await self.template_repository.get(
                session, application.id, request.template, request.channel.value
            )
            if template is None:
                raise TemplateNotFoundError
            if (
                template.status != "active"
                or template.provider != "meta"
                or not template.provider_template_name
                or not template.language_code
            ):
                raise TemplateUnavailableError
            try:
                body_values = ordered_body_values(template.parameter_schema, request.parameters)
            except (InvalidTemplateParametersError, InvalidTemplateSchemaError) as exc:
                raise TemplateParameterError(str(exc)) from exc
            if request.billing_account is not None:
                quote = await self.billing_service.quote_and_lock(
                    session,
                    application_id=application.id,
                    external_id=request.billing_account,
                    channel=request.channel.value,
                    message_kind="template",
                    billing_category=template.billing_category,
                )
            message, created = await self.repository.reserve(
                session,
                application_id=application.id,
                billing_account_id=quote.account.id if quote else None,
                channel=request.channel.value,
                recipient=request.to,
                message_kind="template",
                text_body=None,
                template_name=template.template_key,
                template_parameters=request.parameters,
                provider="meta",
                provider_template_name=template.provider_template_name,
                template_language_code=template.language_code,
                billing_category=getattr(template, "billing_category", None),
                status=OutboundMessageStatus.PENDING,
                source_type=metadata.get("source_type"),
                source_id=metadata.get("source_id"),
                message_metadata=request.metadata,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
            )
            if created and quote is not None:
                await self.billing_service.reserve(session, quote, message.id)
        if not created:
            if message.payload_hash != payload_hash:
                raise IdempotencyConflictError
            return message
        now = datetime.now(UTC)
        try:
            result = await self.client.send_template_message(
                request.to,
                provider_template_name=template.provider_template_name,
                language_code=template.language_code,
                body_parameters=body_values,
            )
        except WhatsAppAPIError as exc:
            failure_status = (
                OutboundMessageStatus.UNCERTAIN
                if exc.ambiguous_delivery
                else OutboundMessageStatus.FAILED
            )
            async with session.begin():
                await self.repository.mark_error(
                    session,
                    message.id,
                    status=failure_status,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    timestamp=now,
                )
                if not exc.ambiguous_delivery and getattr(message, "billing_account_id", None):
                    await self.billing_service.release(session, message.id)
            await session.refresh(message)
            return message
        if result.meta_message_id is None:
            async with session.begin():
                await self.repository.mark_error(
                    session,
                    message.id,
                    status=OutboundMessageStatus.UNCERTAIN,
                    error_code=None,
                    error_message="Provider success response omitted message ID",
                    timestamp=now,
                )
            await session.refresh(message)
            return message
        async with session.begin():
            await self.repository.mark_sent(session, message.id, result.meta_message_id, now)
            if getattr(message, "billing_account_id", None):
                await self.billing_service.charge(session, message, now)
        await session.refresh(message)
        return message
