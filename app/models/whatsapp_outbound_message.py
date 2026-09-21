import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OutboundMessageStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class OutboundMessage(Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sent', 'delivered', 'read', 'failed', 'uncertain')",
            name="outbound_message_status",
        ),
        CheckConstraint("channel IN ('whatsapp', 'sms')", name="outbound_message_channel"),
        CheckConstraint("message_kind IN ('text', 'template')", name="outbound_message_kind"),
        CheckConstraint(
            "billing_mode IN ('customer', 'platform')", name="outbound_message_billing_mode"
        ),
        CheckConstraint(
            "(channel = 'whatsapp' AND sms_character_count IS NULL "
            "AND sms_page_count IS NULL AND provider_route IS NULL) OR "
            "(channel = 'sms' AND sms_character_count BETWEEN 1 AND 960 "
            "AND sms_page_count BETWEEN 1 AND 6 "
            "AND provider_route IN ('standard', 'transactional'))",
            name="outbound_message_sms_pages",
        ),
        UniqueConstraint("application_id", "idempotency_key", name="uq_outbound_app_idempotency"),
        UniqueConstraint("provider", "provider_message_id", name="uq_outbound_provider_message"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messaging_applications.id", ondelete="RESTRICT"),
        nullable=True,
    )
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("billing_accounts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("whatsapp_inbound_messages.id", ondelete="RESTRICT"),
        nullable=True,
    )
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="whatsapp")
    recipient: Mapped[str] = mapped_column(String(32), nullable=False)
    message_kind: Mapped[str] = mapped_column(String(24), nullable=False, default="text")
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    template_parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    provider_template_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    template_language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provider_route: Mapped[str | None] = mapped_column(String(24), nullable=True)
    sms_character_count: Mapped[int | None] = mapped_column(nullable=True)
    sms_page_count: Mapped[int | None] = mapped_column(nullable=True)
    provider_cost_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    billing_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    billing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="customer", server_default="customer"
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="meta")
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OutboundMessageStatus.PENDING
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    message_metadata: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reconciliation_status: Mapped[str | None] = mapped_column(String(32), nullable=True)


WhatsAppOutboundMessage = OutboundMessage
