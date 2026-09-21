import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BillingAccount(Base):
    __tablename__ = "billing_accounts"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'suspended')", name="billing_account_status"),
        CheckConstraint("billing_mode IN ('prepaid')", name="billing_account_mode"),
        CheckConstraint("balance_minor >= 0", name="billing_account_nonnegative_balance"),
        CheckConstraint("reserved_minor >= 0", name="billing_account_nonnegative_reserved"),
        CheckConstraint(
            "reserved_minor <= balance_minor", name="billing_account_reservations_within_balance"
        ),
        UniqueConstraint(
            "application_id", "external_id", name="uq_billing_account_application_external"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messaging_applications.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(String(160), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    billing_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="prepaid")
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PricingRule(Base):
    __tablename__ = "pricing_rules"
    __table_args__ = (
        CheckConstraint("channel IN ('whatsapp', 'sms')", name="pricing_rule_channel"),
        CheckConstraint("message_kind IN ('text', 'template')", name="pricing_rule_kind"),
        CheckConstraint(
            "billing_category IS NULL OR billing_category IN "
            "('utility', 'authentication', 'marketing')",
            name="pricing_rule_category",
        ),
        CheckConstraint("customer_price_minor > 0", name="pricing_rule_positive_price"),
        CheckConstraint("status IN ('active', 'disabled')", name="pricing_rule_status"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="pricing_rule_effective_period",
        ),
        Index(
            "ix_pricing_rules_selection",
            "application_id",
            "channel",
            "message_kind",
            "billing_category",
            "effective_from",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messaging_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    message_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    billing_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    customer_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BillingReservation(Base):
    __tablename__ = "billing_reservations"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="billing_reservation_positive_amount"),
        CheckConstraint(
            "status IN ('active', 'charged', 'released')", name="billing_reservation_status"
        ),
        UniqueConstraint("outbound_message_id", name="uq_billing_reservation_outbound"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    outbound_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbound_messages.id", ondelete="RESTRICT"), nullable=False
    )
    pricing_rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pricing_rules.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        CheckConstraint(
            "type IN ('credit', 'debit', 'adjustment_credit', 'adjustment_debit')",
            name="wallet_transaction_type",
        ),
        CheckConstraint("amount_minor > 0", name="wallet_transaction_positive_amount"),
        UniqueConstraint(
            "billing_account_id",
            "reference_type",
            "reference_id",
            name="uq_wallet_transaction_reference",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    billing_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    reference_type: Mapped[str] = mapped_column(String(40), nullable=False)
    reference_id: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MessageUsage(Base):
    __tablename__ = "message_usage"
    __table_args__ = (
        CheckConstraint("channel IN ('whatsapp', 'sms')", name="message_usage_channel"),
        CheckConstraint("message_kind IN ('text', 'template')", name="message_usage_kind"),
        CheckConstraint("customer_price_minor >= 0", name="message_usage_nonnegative_price"),
        CheckConstraint(
            "billing_status IN ('charged', 'platform_funded')", name="message_usage_status"
        ),
        CheckConstraint(
            "billing_mode IN ('customer', 'platform')", name="message_usage_billing_mode"
        ),
        CheckConstraint(
            "(channel = 'whatsapp' AND sms_page_count IS NULL) OR "
            "(channel = 'sms' AND sms_page_count BETWEEN 1 AND 6)",
            name="message_usage_sms_pages",
        ),
        CheckConstraint(
            "(billing_mode = 'customer' AND billing_account_id IS NOT NULL "
            "AND pricing_rule_id IS NOT NULL AND currency IS NOT NULL "
            "AND customer_price_minor > 0 AND billing_status = 'charged') OR "
            "(billing_mode = 'platform' AND pricing_rule_id IS NULL "
            "AND currency IS NULL AND customer_price_minor = 0 "
            "AND billing_status = 'platform_funded')",
            name="message_usage_funding_consistency",
        ),
        UniqueConstraint("outbound_message_id", name="uq_message_usage_outbound"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messaging_applications.id", ondelete="RESTRICT")
    )
    billing_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("billing_accounts.id", ondelete="RESTRICT"), nullable=True
    )
    outbound_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("outbound_messages.id", ondelete="RESTRICT")
    )
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    message_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    billing_category: Mapped[str | None] = mapped_column(String(24), nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="customer", server_default="customer"
    )
    pricing_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pricing_rules.id", ondelete="RESTRICT"), nullable=True
    )
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    provider_cost_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sms_page_count: Mapped[int | None] = mapped_column(nullable=True)
    customer_price_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    billing_status: Mapped[str] = mapped_column(String(16), nullable=False, default="charged")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
