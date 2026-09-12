import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        CheckConstraint("channel IN ('whatsapp')", name="message_template_channel"),
        CheckConstraint("status IN ('active', 'disabled')", name="message_template_status"),
        CheckConstraint("provider IN ('meta')", name="message_template_provider"),
        CheckConstraint(
            "billing_category IN ('utility', 'authentication', 'marketing')",
            name="message_template_billing_category",
        ),
        UniqueConstraint(
            "application_id",
            "template_key",
            "channel",
            name="uq_message_templates_application_key_channel",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messaging_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_key: Mapped[str] = mapped_column(String(100), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="meta")
    provider_template_name: Mapped[str] = mapped_column(String(512), nullable=False)
    language_code: Mapped[str] = mapped_column(String(16), nullable=False, default="en_US")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    parameter_schema: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    billing_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
