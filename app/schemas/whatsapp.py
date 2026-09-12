from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class InboundWhatsAppMessage(BaseModel):
    """The stable subset of an inbound message that the application persists."""

    model_config = ConfigDict(extra="ignore")

    meta_message_id: str
    sender_wa_id: str
    recipient_phone_number_id: str
    message_type: str
    text_body: str | None = None
    message_timestamp: datetime
    raw_payload: dict[str, Any]


class WhatsAppSendResult(BaseModel):
    recipient: str
    meta_message_id: str | None = None
    success: bool
