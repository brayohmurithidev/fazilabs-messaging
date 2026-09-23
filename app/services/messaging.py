import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.messaging_application import MessagingApplication
from app.models.whatsapp_outbound_message import OutboundMessage, OutboundMessageStatus
from app.repositories.message_templates import MessageTemplateRepository
from app.repositories.outbound_messages import OutboundMessageRepository
from app.schemas.messages import TemplateMessageRequest, TextMessageRequest
from app.services.advanta_client import AdvantaAPIError, AdvantaClient
from app.services.billing import BillingQuote, BillingService
from app.services.sms import InvalidSmsError, analyze_sms, render_sms
from app.services.template_parameters import (
    InvalidTemplateParametersError,
    InvalidTemplateSchemaError,
    ordered_template_values,
    validate_parameter_schema,
)
from app.services.whatsapp_client import WhatsAppAPIError, WhatsAppCloudAPIClient

logger = logging.getLogger(__name__)

DEFAULT_CLAIM_LEASE_SECONDS = 90


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


class ProviderUnavailableError(Exception):
    pass


def canonical_payload_hash(payload: TextMessageRequest | TemplateMessageRequest) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


ProviderCall = Callable[[], Awaitable[str | None]]


class MessagingService:
    def __init__(
        self,
        client: WhatsAppCloudAPIClient | None,
        repository: OutboundMessageRepository | None = None,
        template_repository: MessageTemplateRepository | None = None,
        billing_service: BillingService | None = None,
        advanta_client: AdvantaClient | None = None,
        advanta_provider_cost_per_page_minor: int | None = None,
        *,
        claim_identity: str | None = None,
        claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> None:
        self.client = client
        self.repository = repository or OutboundMessageRepository()
        self.template_repository = template_repository or MessageTemplateRepository()
        self.billing_service = billing_service or BillingService()
        self.advanta_client = advanta_client
        self.advanta_provider_cost_per_page_minor = advanta_provider_cost_per_page_minor
        self.claim_identity = claim_identity or f"api:{uuid4().hex[:12]}"
        self.claim_lease_seconds = claim_lease_seconds

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
            if self.client is None:
                raise ProviderUnavailableError
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
                billing_mode="customer",
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

        message = await self._claim_or_return_current(session, message)
        if message.status != OutboundMessageStatus.SUBMITTING:
            return message

        async def call_provider() -> str | None:
            result = await self.client.send_text_message(request.to, request.text)
            return result.meta_message_id

        return await self._dispatch_and_record(session, message, call_provider)

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
        attributed_account = None
        async with session.begin():
            existing = await self.repository.get_by_idempotency_key(
                session, application.id, idempotency_key
            )
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    raise IdempotencyConflictError
                return existing
            template = await self.template_repository.get(
                session, application.id, request.template, request.channel.value
            )
            if template is None:
                raise TemplateNotFoundError
            if template.status != "active":
                raise TemplateUnavailableError
            is_whatsapp = request.channel.value == "whatsapp"
            if is_whatsapp and (
                self.client is None
                or template.provider != "meta"
                or not template.provider_template_name
                or not template.language_code
            ):
                if self.client is None:
                    raise ProviderUnavailableError
                raise TemplateUnavailableError
            if not is_whatsapp and (
                template.provider != "advanta"
                or not template.sms_body
                or template.provider_route not in {"standard", "transactional"}
                or self.advanta_client is None
            ):
                if self.advanta_client is None:
                    raise ProviderUnavailableError
                raise TemplateUnavailableError
            billing_mode = getattr(template, "billing_mode", "customer")
            if billing_mode not in {"customer", "platform"}:
                raise TemplateUnavailableError
            if (
                billing_mode == "customer"
                and getattr(application, "billing_required", False)
                and request.billing_account is None
            ):
                raise BillingAccountRequiredError
            try:
                template_values = ordered_template_values(
                    template.parameter_schema, request.parameters
                )
                rendered_sms = None
                sms_analysis = None
                if not is_whatsapp:
                    rendered_sms = render_sms(
                        template.sms_body,
                        validate_parameter_schema(template.parameter_schema)["body"],
                        request.parameters,
                    )
                    sms_analysis = analyze_sms(rendered_sms)
            except (
                InvalidTemplateParametersError,
                InvalidTemplateSchemaError,
                InvalidSmsError,
            ) as exc:
                raise TemplateParameterError(str(exc)) from exc
            if billing_mode == "customer" and request.billing_account is not None:
                quote = await self.billing_service.quote_and_lock(
                    session,
                    application_id=application.id,
                    external_id=request.billing_account,
                    channel=request.channel.value,
                    message_kind="template",
                    billing_category=template.billing_category,
                    units=sms_analysis.page_count if sms_analysis else 1,
                )
            elif billing_mode == "platform" and request.billing_account is not None:
                attributed_account = await self.billing_service.resolve_account(
                    session,
                    application_id=application.id,
                    external_id=request.billing_account,
                )
            message, created = await self.repository.reserve(
                session,
                application_id=application.id,
                billing_account_id=(
                    quote.account.id
                    if quote
                    else attributed_account.id
                    if attributed_account
                    else None
                ),
                channel=request.channel.value,
                recipient=request.to,
                message_kind="template",
                text_body=rendered_sms,
                template_name=template.template_key,
                template_parameters=request.parameters,
                provider=template.provider,
                provider_template_name=template.provider_template_name,
                template_language_code=template.language_code,
                provider_route=getattr(template, "provider_route", None),
                sms_character_count=sms_analysis.character_count if sms_analysis else None,
                sms_page_count=sms_analysis.page_count if sms_analysis else None,
                provider_cost_minor=(
                    sms_analysis.page_count * self.advanta_provider_cost_per_page_minor
                    if sms_analysis and self.advanta_provider_cost_per_page_minor is not None
                    else None
                ),
                billing_category=getattr(template, "billing_category", None),
                billing_mode=billing_mode,
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

        message = await self._claim_or_return_current(session, message)
        if message.status != OutboundMessageStatus.SUBMITTING:
            return message

        if is_whatsapp:

            async def call_provider() -> str | None:
                result = await self.client.send_template_message(
                    request.to,
                    provider_template_name=template.provider_template_name,
                    language_code=template.language_code,
                    body_parameters=template_values.body,
                    url_button_parameters=template_values.url_buttons,
                )
                return result.meta_message_id
        else:

            async def call_provider() -> str | None:
                result = await self.advanta_client.send(
                    mobile=request.to, message=rendered_sms, route=template.provider_route
                )
                return result.provider_message_id

        return await self._dispatch_and_record(session, message, call_provider)

    async def dispatch_claimed_from_row(
        self, session: AsyncSession, message: OutboundMessage
    ) -> OutboundMessage:
        """Resume a message that is already durably `submitting`.

        Used by the recovery sweeper to submit a message whose original
        request process never reached the provider call. Every provider-call
        argument is reconstructed purely from the persisted row -- never from
        data that only ever lived in the original, now-gone, request.
        """
        if message.status != OutboundMessageStatus.SUBMITTING:
            return message
        if message.message_kind == "text":
            if self.client is None:
                return await self._record_unrecoverable(session, message)

            async def call_provider() -> str | None:
                result = await self.client.send_text_message(message.recipient, message.text_body)
                return result.meta_message_id
        elif message.channel == "whatsapp":
            if self.client is None:
                return await self._record_unrecoverable(session, message)
            template = await self.template_repository.get(
                session, message.application_id, message.template_name, "whatsapp"
            )
            if (
                template is None
                or template.status != "active"
                or template.provider != "meta"
                or not template.provider_template_name
                or not template.language_code
            ):
                return await self._record_unrecoverable(session, message)
            try:
                template_values = ordered_template_values(
                    template.parameter_schema, message.template_parameters or {}
                )
            except (InvalidTemplateParametersError, InvalidTemplateSchemaError):
                return await self._record_unrecoverable(session, message)

            async def call_provider() -> str | None:
                result = await self.client.send_template_message(
                    message.recipient,
                    provider_template_name=template.provider_template_name,
                    language_code=template.language_code,
                    body_parameters=template_values.body,
                    url_button_parameters=template_values.url_buttons,
                )
                return result.meta_message_id
        else:
            if self.advanta_client is None or not message.text_body or not message.provider_route:
                return await self._record_unrecoverable(session, message)

            async def call_provider() -> str | None:
                result = await self.advanta_client.send(
                    mobile=message.recipient,
                    message=message.text_body,
                    route=message.provider_route,
                )
                return result.provider_message_id

        return await self._dispatch_and_record(session, message, call_provider)

    async def _claim_or_return_current(
        self, session: AsyncSession, message: OutboundMessage
    ) -> OutboundMessage:
        async with session.begin():
            claimed = await self.repository.claim(
                session,
                message.id,
                claimed_by=self.claim_identity,
                lease_seconds=self.claim_lease_seconds,
            )
        if claimed is None:
            logger.info(
                "outbound_claim_lost",
                extra={"outbound_message_id": str(message.id), "claimed_by": self.claim_identity},
            )
            return await self._reload(session, message.id)
        logger.info(
            "outbound_claimed",
            extra={
                "outbound_message_id": str(claimed.id),
                "claimed_by": self.claim_identity,
                "status_from": "pending",
                "status_to": "submitting",
                "channel": claimed.channel,
            },
        )
        return claimed

    async def _dispatch_and_record(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        call_provider: ProviderCall,
    ) -> OutboundMessage:
        async with session.begin():
            started = await self.repository.start_attempt(
                session,
                message.id,
                claimed_by=message.claimed_by,
                lease_seconds=self.claim_lease_seconds,
            )
        if started is None:
            logger.warning(
                "outbound_attempt_not_started",
                extra={"outbound_message_id": str(message.id), "claimed_by": message.claimed_by},
            )
            return await self._reload(session, message.id)
        now = datetime.now(UTC)
        try:
            provider_message_id = await call_provider()
        except (WhatsAppAPIError, AdvantaAPIError) as exc:
            return await self._record_provider_failure(session, message, exc, now)
        if provider_message_id is None:
            return await self._record_ambiguous_success(session, message, now)
        return await self._record_provider_success(session, message, provider_message_id, now)

    async def _record_provider_success(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        provider_message_id: str,
        timestamp: datetime,
    ) -> OutboundMessage:
        async with session.begin():
            updated = await self.repository.mark_sent(
                session, message.id, provider_message_id, timestamp
            )
            if updated is not None:
                if getattr(updated, "billing_mode", "customer") == "platform":
                    await self.billing_service.record_platform_usage(session, updated)
                elif getattr(updated, "billing_account_id", None):
                    await self.billing_service.charge(session, updated, timestamp)
        result = updated if updated is not None else await self._reload(session, message.id)
        logger.info(
            "outbound_status_transition",
            extra={
                "outbound_message_id": str(result.id),
                "status_from": "submitting",
                "status_to": result.status,
                "provider_message_id": provider_message_id,
                "guard_matched": updated is not None,
            },
        )
        return result

    async def _record_provider_failure(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        exc: WhatsAppAPIError | AdvantaAPIError,
        timestamp: datetime,
    ) -> OutboundMessage:
        failure_status = (
            OutboundMessageStatus.UNCERTAIN
            if exc.ambiguous_delivery
            else OutboundMessageStatus.FAILED
        )
        async with session.begin():
            updated = await self.repository.mark_error(
                session,
                message.id,
                status=failure_status,
                error_code=exc.error_code,
                error_message=str(exc),
                timestamp=timestamp,
            )
            if (
                updated is not None
                and not exc.ambiguous_delivery
                and getattr(updated, "billing_mode", "customer") == "customer"
                and getattr(updated, "billing_account_id", None)
            ):
                await self.billing_service.release(session, message.id)
        result = updated if updated is not None else await self._reload(session, message.id)
        logger.info(
            "outbound_status_transition",
            extra={
                "outbound_message_id": str(result.id),
                "status_from": "submitting",
                "status_to": result.status,
                "error_code": exc.error_code,
                "guard_matched": updated is not None,
            },
        )
        return result

    async def _record_ambiguous_success(
        self, session: AsyncSession, message: OutboundMessage, timestamp: datetime
    ) -> OutboundMessage:
        async with session.begin():
            updated = await self.repository.mark_error(
                session,
                message.id,
                status=OutboundMessageStatus.UNCERTAIN,
                error_code=None,
                error_message="Provider success response omitted message ID",
                timestamp=timestamp,
            )
        result = updated if updated is not None else await self._reload(session, message.id)
        logger.info(
            "outbound_status_transition",
            extra={
                "outbound_message_id": str(result.id),
                "status_from": "submitting",
                "status_to": result.status,
            },
        )
        return result

    async def _record_unrecoverable(
        self, session: AsyncSession, message: OutboundMessage
    ) -> OutboundMessage:
        async with session.begin():
            updated = await self.repository.mark_error(
                session,
                message.id,
                status=OutboundMessageStatus.UNCERTAIN,
                error_code=None,
                error_message="Recovery dispatch could not resolve provider or template state",
                timestamp=datetime.now(UTC),
            )
        result = updated if updated is not None else await self._reload(session, message.id)
        logger.warning(
            "outbound_recovery_unresolvable",
            extra={"outbound_message_id": str(result.id)},
        )
        return result

    async def _reload(self, session: AsyncSession, message_id) -> OutboundMessage:
        async with session.begin():
            return await session.get(OutboundMessage, message_id, populate_existing=True)
