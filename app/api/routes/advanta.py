import json
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.advanta_webhook import AdvantaWebhookService

router = APIRouter(prefix="/webhooks/advanta", tags=["Webhooks"])


@router.post(
    "",
    summary="Receive Advanta delivery reports",
    description=(
        "Provider-facing receiver for Advanta SMS delivery reports in JSON or form-encoded form. "
        "Delivery updates are matched by provider message ID and handled idempotently. Advanta "
        "does not currently provide a verified webhook signature in this integration; restrict "
        "this route at the network/reverse-proxy layer where practical."
    ),
    response_description="The delivery report was safely accepted (including unknown IDs/states).",
)
async def receive_advanta_webhook(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> dict[str, str]:
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = dict(parse_qsl(raw.decode("utf-8", errors="replace"), keep_blank_values=True))
    await AdvantaWebhookService().accept(session, payload)
    return {"status": "accepted"}
