import uuid
from datetime import datetime

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
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MessageReconciliationAttempt(Base):
    __tablename__ = "message_reconciliation_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('resolved_accepted', 'resolved_rejected', 'unresolved', 'released')",
            name="recon_attempt_status",
        ),
        CheckConstraint("method IN ('operator', 'webhook')", name="recon_attempt_method"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    outbound_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbound_messages.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BillingException(Base):
    __tablename__ = "billing_exceptions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('late_provider_acceptance_after_release')",
            name="billing_exception_type",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved_charged', 'resolved_waived')",
            name="billing_exception_status",
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL AND resolution_reason IS NULL) OR "
            "(status != 'open' AND resolved_at IS NOT NULL AND resolution_reason IS NOT NULL)",
            name="billing_exception_resolution_fields",
        ),
        CheckConstraint("amount_minor > 0", name="billing_exception_positive_amount"),
        UniqueConstraint("outbound_message_id", "type", name="uq_billing_exception_outbound_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id", ondelete="RESTRICT"), index=True
    )
    outbound_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbound_messages.id", ondelete="RESTRICT")
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
