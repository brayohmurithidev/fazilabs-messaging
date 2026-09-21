import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.whatsapp_webhook import (
    WhatsAppWebhookService,
    verify_signature,
    verify_subscription,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks/whatsapp", tags=["Webhooks"])


def get_webhook_service(request: Request) -> WhatsAppWebhookService:
    return WhatsAppWebhookService()


@router.get(
    "",
    summary="Verify the Meta webhook",
    description=(
        "Provider-facing Meta subscription verification endpoint. Meta supplies the verification "
        "query parameters; a matching server-side verify token returns the plain-text challenge."
    ),
    response_description="The plain-text Meta challenge when verification succeeds.",
    responses={403: {"description": "Verification token or mode did not match."}},
)
async def verify_webhook(
    request: Request,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> Response:
    settings = request.app.state.settings
    if challenge is not None and verify_subscription(
        mode, verify_token, settings.whatsapp_verify_token
    ):
        logger.info("WhatsApp webhook verification succeeded")
        return Response(content=challenge, media_type="text/plain")

    logger.warning("WhatsApp webhook verification failed")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")


@router.post(
    "",
    summary="Receive Meta webhook events",
    description=(
        "Provider-facing receiver for inbound WhatsApp messages and outbound delivery/read "
        "updates. The raw body must carry a valid `X-Hub-Signature-256` generated with the "
        "configured Meta app secret. Duplicate events are handled idempotently."
    ),
    response_description="The verified event was accepted for synchronous processing.",
    responses={
        400: {"description": "The signed body is not valid JSON."},
        401: {"description": "The Meta signature is missing or invalid."},
    },
)
async def receive_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    service: Annotated[WhatsAppWebhookService, Depends(get_webhook_service)],
) -> dict[str, str]:
    raw_body = await request.body()
    logger.info("WhatsApp webhook received")

    signature = request.headers.get("X-Hub-Signature-256")
    if not verify_signature(raw_body, signature, request.app.state.settings.whatsapp_app_secret):
        logger.warning("WhatsApp webhook signature validation failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from exc

    await service.accept_payload(payload, session)
    return {"status": "accepted"}
