import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
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
from app.services.billing import (
    AmbiguousPricingRuleError,
    BillingAccountNotFoundError,
    BillingAccountSuspendedError,
    InsufficientBalanceError,
    PricingRuleNotFoundError,
)
from app.services.messaging import (
    IdempotencyConflictError,
    MessagingService,
    TemplateNotFoundError,
    TemplateParameterError,
    TemplateUnavailableError,
)
from app.services.whatsapp_client import WhatsAppCloudAPIClient

router = APIRouter(prefix="/api/v1/messages", tags=["messages"])


def billing_http_error(exc: Exception) -> HTTPException:
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
    if not all(
        (
            settings.whatsapp_api_version,
            settings.whatsapp_phone_number_id,
            settings.whatsapp_access_token,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WhatsApp provider is not configured",
        )
    return MessagingService(
        WhatsAppCloudAPIClient(
            api_version=settings.whatsapp_api_version,
            phone_number_id=settings.whatsapp_phone_number_id,
            access_token=settings.whatsapp_access_token,
        )
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
    )


@router.post("/text", response_model=MessageResponse, summary="Send a WhatsApp text message")
async def send_text_message(
    body: TextMessageRequest,
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MessagingService, Depends(get_messaging_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MessageResponse:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", idempotency_key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
    try:
        message = await service.send_text(session, application, idempotency_key, body)
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409, detail="Idempotency key was used with a different payload"
        ) from exc
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
    "/template", response_model=MessageResponse, summary="Send an approved WhatsApp template"
)
async def send_template_message(
    body: TemplateMessageRequest,
    application: AuthenticatedApplication,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[MessagingService, Depends(get_messaging_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MessageResponse:
    if idempotency_key is None:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", idempotency_key):
        raise HTTPException(status_code=400, detail="Invalid Idempotency-Key")
    try:
        message = await service.send_template(session, application, idempotency_key, body)
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
    except (
        BillingAccountNotFoundError,
        BillingAccountSuspendedError,
        InsufficientBalanceError,
        PricingRuleNotFoundError,
        AmbiguousPricingRuleError,
    ) as exc:
        raise billing_http_error(exc) from exc
    return response_for(message, application.slug)


@router.get("/{message_id}", response_model=MessageResponse, summary="Get a message")
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


@router.get("", response_model=MessageListResponse, summary="List application messages")
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
