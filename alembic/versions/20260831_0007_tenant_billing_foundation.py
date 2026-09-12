"""Add tenant metering and prepaid billing foundation.

Revision ID: 20260831_0007
Revises: 20260829_0006
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260831_0007"
down_revision: str | Sequence[str] | None = "20260829_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("message_templates", sa.Column("billing_category", sa.String(24)))
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_billing_category"),
        "message_templates",
        "billing_category IN ('utility', 'authentication', 'marketing')",
    )
    op.execute(
        "UPDATE message_templates SET billing_category = 'utility' "
        "WHERE template_key = 'student_results_ready'"
    )
    op.create_table(
        "billing_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.String(160), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("billing_mode", sa.String(16), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column("reserved_minor", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active', 'suspended')",
            name=op.f("ck_billing_accounts_billing_account_status"),
        ),
        sa.CheckConstraint(
            "billing_mode IN ('prepaid')", name=op.f("ck_billing_accounts_billing_account_mode")
        ),
        sa.CheckConstraint(
            "balance_minor >= 0",
            name=op.f("ck_billing_accounts_billing_account_nonnegative_balance"),
        ),
        sa.CheckConstraint(
            "reserved_minor >= 0",
            name=op.f("ck_billing_accounts_billing_account_nonnegative_reserved"),
        ),
        sa.CheckConstraint(
            "reserved_minor <= balance_minor",
            name=op.f("ck_billing_accounts_billing_account_reservations_within_balance"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["messaging_applications.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "application_id", "external_id", name="uq_billing_account_application_external"
        ),
    )
    op.create_index(
        op.f("ix_billing_accounts_application_id"), "billing_accounts", ["application_id"]
    )
    op.create_table(
        "pricing_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("message_kind", sa.String(24), nullable=False),
        sa.Column("billing_category", sa.String(24)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("customer_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "channel IN ('whatsapp')", name=op.f("ck_pricing_rules_pricing_rule_channel")
        ),
        sa.CheckConstraint(
            "message_kind IN ('text', 'template')", name=op.f("ck_pricing_rules_pricing_rule_kind")
        ),
        sa.CheckConstraint(
            "billing_category IS NULL OR billing_category IN "
            "('utility', 'authentication', 'marketing')",
            name=op.f("ck_pricing_rules_pricing_rule_category"),
        ),
        sa.CheckConstraint(
            "customer_price_minor > 0", name=op.f("ck_pricing_rules_pricing_rule_positive_price")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')", name=op.f("ck_pricing_rules_pricing_rule_status")
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name=op.f("ck_pricing_rules_pricing_rule_effective_period"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["messaging_applications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pricing_rules_selection",
        "pricing_rules",
        ["application_id", "channel", "message_kind", "billing_category", "effective_from"],
    )
    op.add_column(
        "outbound_messages", sa.Column("billing_account_id", postgresql.UUID(as_uuid=True))
    )
    op.add_column("outbound_messages", sa.Column("billing_category", sa.String(24)))
    op.create_foreign_key(
        op.f("fk_outbound_messages_billing_account_id_billing_accounts"),
        "outbound_messages",
        "billing_accounts",
        ["billing_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_outbound_messages_billing_account_id"), "outbound_messages", ["billing_account_id"]
    )
    op.create_table(
        "billing_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pricing_rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f("ck_billing_reservations_billing_reservation_positive_amount"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'charged', 'released')",
            name=op.f("ck_billing_reservations_billing_reservation_status"),
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"], ["billing_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["outbound_message_id"], ["outbound_messages.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["pricing_rule_id"], ["pricing_rules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outbound_message_id", name="uq_billing_reservation_outbound"),
    )
    op.create_table(
        "wallet_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(24), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reference_type", sa.String(40), nullable=False),
        sa.Column("reference_id", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "type IN ('credit', 'debit', 'adjustment_credit', 'adjustment_debit')",
            name=op.f("ck_wallet_transactions_wallet_transaction_type"),
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f("ck_wallet_transactions_wallet_transaction_positive_amount"),
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"], ["billing_accounts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "billing_account_id",
            "reference_type",
            "reference_id",
            name="uq_wallet_transaction_reference",
        ),
    )
    op.create_table(
        "message_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("message_kind", sa.String(24), nullable=False),
        sa.Column("billing_category", sa.String(24)),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("pricing_rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("provider_cost_minor", sa.BigInteger()),
        sa.Column("customer_price_minor", sa.BigInteger(), nullable=False),
        sa.Column("billing_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "channel IN ('whatsapp')", name=op.f("ck_message_usage_message_usage_channel")
        ),
        sa.CheckConstraint(
            "message_kind IN ('text', 'template')", name=op.f("ck_message_usage_message_usage_kind")
        ),
        sa.CheckConstraint(
            "customer_price_minor > 0", name=op.f("ck_message_usage_message_usage_positive_price")
        ),
        sa.CheckConstraint(
            "billing_status IN ('charged')", name=op.f("ck_message_usage_message_usage_status")
        ),
        sa.ForeignKeyConstraint(
            ["application_id"], ["messaging_applications.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"], ["billing_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["outbound_message_id"], ["outbound_messages.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["pricing_rule_id"], ["pricing_rules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outbound_message_id", name="uq_message_usage_outbound"),
    )


def downgrade() -> None:
    op.drop_table("message_usage")
    op.drop_table("wallet_transactions")
    op.drop_table("billing_reservations")
    op.drop_index(op.f("ix_outbound_messages_billing_account_id"), table_name="outbound_messages")
    op.drop_constraint(
        op.f("fk_outbound_messages_billing_account_id_billing_accounts"),
        "outbound_messages",
        type_="foreignkey",
    )
    op.drop_column("outbound_messages", "billing_category")
    op.drop_column("outbound_messages", "billing_account_id")
    op.drop_index("ix_pricing_rules_selection", table_name="pricing_rules")
    op.drop_table("pricing_rules")
    op.drop_index(op.f("ix_billing_accounts_application_id"), table_name="billing_accounts")
    op.drop_table("billing_accounts")
    op.drop_constraint(
        op.f("ck_message_templates_message_template_billing_category"),
        "message_templates",
        type_="check",
    )
    op.drop_column("message_templates", "billing_category")
