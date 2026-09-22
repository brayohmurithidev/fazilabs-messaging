import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedApplication
from app.db.session import get_db_session
from app.models.whatsapp_outbound_message import OutboundMessage
from app.repositories.outbound_messages import OutboundMessageRepository
from app.schemas.messages import (
    MessageListResponse,
    MessageResponse,
    TemplateMessageRequest,
    TextMessageRequest,
)
from app.services.advanta_client import AdvantaClient
from app.services.billing import (
    AmbiguousPricingRuleError,
    BillingAccountNotFoundError,
    BillingAccountSuspendedError,
    InsufficientBalanceError,
    PricingRuleNotFoundError,
)
from app.services.messaging import (
    BillingAccountRequiredError,
    IdempotencyConflictError,
    MessagingService,
    ProviderUnavailableError,
    TemplateNotFoundError,
    TemplateParameterError,
    TemplateUnavailableError,
)
from app.services.whatsapp_client import WhatsAppCloudAPIClient

router = APIRouter(prefix="/api/v1/messages", tags=["Messages"])

IDEMPOTENCY_DESCRIPTION = (
    "Required caller-generated, non-secret identity for one logical send (8-200 characters). "
    "Retry with the same key and identical request to reuse the original message without another "
    "provider call or charge. Reusing it with a changed request returns HTTP 409."
)

SEND_RESPONSES = {
    400: {"description": "The Idempotency-Key header is missing or invalid."},
    401: {"description": "Missing or invalid application API key."},
    402: {"description": "The customer-funded billing account has insufficient balance."},
    403: {"description": "The application or billing account is disabled/suspended."},
    404: {"description": "The billing account or semantic template was not found."},
    409: {"description": "Idempotency conflict, unavailable template, or ambiguous pricing."},
    422: {
        "description": (
            "Request, template parameters, recipient, or billing requirement failed validation."
        )
    },
    503: {"description": "The selected message provider is not configured."},
}


def billing_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BillingAccountRequiredError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "billing_account_required",
                "message": "billing_account is required for this application.",
            },
        )
    if isinstance(exc, BillingAccountNotFoundError):
        return HTTPException(status_code=404, detail="Billing account not found")
    if isinstance(exc, BillingAccountSuspendedError):
        return HTTPException(
            status_code=403,
            detail={"code": "billing_account_suspended", "message": "Billing account suspended."},
        )
    if isinstance(exc, InsufficientBalanceError):
        return HTTPException(
            status_code=402,
            detail={
                "code": "insufficient_messaging_balance",
                "message": "Insufficient messaging balance.",
            },
        )
    return HTTPException(status_code=409, detail="No unambiguous active pricing rule")


def get_messaging_service(request: Request) -> MessagingService:
    settings = request.app.state.settings
    whatsapp = None
    if all(
        (
            settings.whatsapp_api_version,
            settings.whatsapp_phone_number_id,
            settings.whatsapp_access_token,
        )
    ):
        whatsapp = WhatsAppCloudAPIClient(
            api_version=settings.whatsapp_api_version,
            phone_number_id=settings.whatsapp_phone_number_id,
            access_token=settings.whatsapp_access_token,
        )
    advanta = None
    if settings.advanta_base_url and settings.advanta_api_key and settings.advanta_partner_id:
        advanta = AdvantaClient(
            base_url=settings.advanta_base_url,
            api_key=settings.advanta_api_key,
            partner_id=settings.advanta_partner_id,
            sender_id=settings.advanta_sender_id,
        )
    return MessagingService(
        whatsapp,
        advanta_client=advanta,
        advanta_provider_cost_per_page_minor=settings.advanta_provider_cost_per_page_minor,
        claim_lease_seconds=settings.dispatch_claim_lease_seconds,
    )


def response_for(message: OutboundMessage, application_slug: str) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        application=application_slug,
        channel=message.channel,
        status=message.status,
        recipient=message.recipient,
        message_kind=message.message_kind,
        template=message.template_name,
        provider_message_id=message.provider_message_id,
        source_type=message.source_type,
        source_id=message.source_id,
        metadata=message.message_metadata,
        created_at=message.created_at,
        sent_at=message.sent_at,
        delivered_at=message.delivered_at,
        read_at=message.read_at,
        failed_at=message.failed_at,
        sms_character_count=getattr(message, "sms_character_count", None),
        sms_page_count=getattr(message, "sms_page_count", None),
    )


@router.post(
    "/text",
    response_model=MessageResponse,
    summary="Send a WhatsApp text message",
    description=(
        "Sends provider-independent free-form text over WhatsApp. SMS text sends are not "
        "supported; SMS is template-only. Billing is governed by the authenticated application's "
        "billing policy. The response may be `uncertain` when provider acceptance cannot safely "
        "be determined; do not retry with a new idempotency key."
    ),
    response_description="The new message, or the original message on an exact idempotent replay.",
    responses=SEND_RESPONSES,
)
async def send_text_message(
    body: Annotated[TextMessageRequest, Body()],
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MessagingService, Depends(get_messaging_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", description=IDEMPOTENCY_DESCRIPTION)
    ] = None,
) -> MessageResponse:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", idempotency_key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
    try:
        message = await service.send_text(session, application, idempotency_key, body)
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Message provider is not configured") from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Idempotency key was used with a different payload"
        ) from exc
    except BillingAccountRequiredError as exc:
        raise billing_http_error(exc) from exc
    except (
        BillingAccountNotFoundError,
        BillingAccountSuspendedError,
        InsufficientBalanceError,
        PricingRuleNotFoundError,
        AmbiguousPricingRuleError,
    ) as exc:
        raise billing_http_error(exc) from exc
    return response_for(message, application.slug)


@router.post(
    "/template",
    response_model=MessageResponse,
    summary="Send a semantic message template",
    description=(
        "Resolves an application-scoped semantic template for the requested channel, validates "
        "its named parameters, and sends it through the configured provider. WhatsApp mappings "
        "may contain approved BODY and dynamic URL-button parameters. SMS mappings render and "
        "analyze text server-side (160 characters per page, at most 6 pages/960 characters; "
        "emoji are rejected). Billing mode and the SMS standard/transactional route come from "
        "the mapping and cannot be selected by the caller."
    ),
    response_description="The new message, or the original message on an exact idempotent replay.",
    responses=SEND_RESPONSES,
)
async def send_template_message(
    body: Annotated[
        TemplateMessageRequest,
        Body(
            openapi_examples={
                "whatsapp": {
                    "summary": "WhatsApp approved template",
                    "value": {
                        "channel": "whatsapp",
                        "to": "254700000000",
                        "template": "student_results_ready",
                        "parameters": {
                            "parent_name": "Amina",
                            "student_name": "Baraka",
                            "term": "Term 2",
                            "results_path": "example-access-token",
                        },
                        "billing_account": "school-example-001",
                        "metadata": {
                            "source_type": "student_result",
                            "source_id": "result-example-001",
                        },
                    },
                },
                "sms": {
                    "summary": "Advanta SMS template",
                    "value": {
                        "channel": "sms",
                        "to": "254700000000",
                        "template": "student_results_ready",
                        "parameters": {
                            "parent_name": "Amina",
                            "student_name": "Baraka",
                            "term": "Term 2",
                            "results_url": ("https://example.com/results/example-access-token"),
                        },
                        "billing_account": "school-example-001",
                        "metadata": {
                            "source_type": "student_result",
                            "source_id": "result-example-001",
                        },
                    },
                },
            }
        ),
    ],
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MessagingService, Depends(get_messaging_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", description=IDEMPOTENCY_DESCRIPTION)
    ] = None,
) -> MessageResponse:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", idempotency_key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
    try:
        message = await service.send_template(session, application, idempotency_key, body)
    except ProviderUnavailableError as exc:
        raise HTTPException(status_code=503, detail="Message provider is not configured") from exc
    except TemplateNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Template not found") from exc
    except TemplateUnavailableError as exc:
        raise HTTPException(status_code=409, detail="Template is disabled or unavailable") from exc
    except TemplateParameterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Idempotency key was used with a different payload"
        ) from exc
    except BillingAccountRequiredError as exc:
        raise billing_http_error(exc) from exc
    except (
        BillingAccountNotFoundError,
        BillingAccountSuspendedError,
        InsufficientBalanceError,
        PricingRuleNotFoundError,
        AmbiguousPricingRuleError,
    ) as exc:
        raise billing_http_error(exc) from exc
    return response_for(message, application.slug)


@router.get(
    "/{message_id}",
    response_model=MessageResponse,
    summary="Get a message",
    description=(
        "Gets one message owned by the authenticated application. Statuses are `pending`, "
        "`submitting`, `sent`, `delivered`, `read`, `failed`, or `uncertain`. `submitting` "
        "means a provider submission may be in flight or its outcome is not yet durably "
        "known; `uncertain` means provider acceptance could not safely be determined. "
        "Messaging will not blindly resend a `submitting` or `uncertain` message."
    ),
    response_description="The application-owned message and its current lifecycle status.",
    responses={
        401: {"description": "Missing or invalid application API key."},
        404: {"description": "Message not found for this application."},
    },
)
async def get_message(
    message_id: UUID,
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> MessageResponse:
    message = await OutboundMessageRepository().get_for_application(
        session, application.id, message_id
    )
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return response_for(message, application.slug)


@router.get(
    "",
    response_model=MessageListResponse,
    summary="List application messages",
    description=(
        "Lists messages owned by the authenticated application, newest first, with optional "
        "lifecycle, channel, source, and creation-time filters."
    ),
    response_description="A paginated application-scoped message list.",
    responses={401: {"description": "Missing or invalid application API key."}},
)
async def list_messages(
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    message_status: Annotated[str | None, Query(alias="status")] = None,
    channel: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageListResponse:
    filters = []
    for column, value in (
        (OutboundMessage.status, message_status),
        (OutboundMessage.channel, channel),
        (OutboundMessage.source_type, source_type),
        (OutboundMessage.source_id, source_id),
    ):
        if value is not None:
            filters.append(column == value)
    if created_from is not None:
        filters.append(OutboundMessage.created_at >= created_from)
    if created_to is not None:
        filters.append(OutboundMessage.created_at <= created_to)
    items, total = await OutboundMessageRepository().list_for_application(
        session, application.id, filters=filters, limit=limit, offset=offset
    )
    return MessageListResponse(
        items=[response_for(item, application.slug) for item in items],
        limit=limit,
        offset=offset,
        total=total,
    )
