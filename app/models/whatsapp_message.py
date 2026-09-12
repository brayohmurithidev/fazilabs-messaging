import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WhatsAppInboundMessage(Base):
    __tablename__ = "whatsapp_inbound_messages"
    __table_args__ = (
        UniqueConstraint("meta_message_id", name="uq_whatsapp_inbound_messages_meta_message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    meta_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_wa_id: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient_phone_number_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
