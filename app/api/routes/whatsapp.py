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
router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp-webhooks"])


def get_webhook_service(request: Request) -> WhatsAppWebhookService:
    return WhatsAppWebhookService()


@router.get("")
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


@router.post("")
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
