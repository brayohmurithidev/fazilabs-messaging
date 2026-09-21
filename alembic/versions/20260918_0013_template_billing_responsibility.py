"""Add template billing responsibility and immutable usage attribution.

Revision ID: 20260918_0013
Revises: 20260912_0012
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260918_0013"
down_revision: str | Sequence[str] | None = "20260912_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_templates",
        sa.Column("billing_mode", sa.String(16), server_default="customer", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_billing_mode"),
        "message_templates",
        "billing_mode IN ('customer', 'platform')",
    )
    op.add_column(
        "outbound_messages",
        sa.Column("billing_mode", sa.String(16), server_default="customer", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_billing_mode"),
        "outbound_messages",
        "billing_mode IN ('customer', 'platform')",
    )
    op.add_column(
        "message_usage",
        sa.Column("billing_mode", sa.String(16), server_default="customer", nullable=False),
    )
    op.alter_column("message_usage", "billing_account_id", nullable=True)
    op.alter_column("message_usage", "pricing_rule_id", nullable=True)
    op.alter_column("message_usage", "currency", nullable=True)
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_positive_price"),
        "message_usage",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_status"),
        "message_usage",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_nonnegative_price"),
        "message_usage",
        "customer_price_minor >= 0",
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_status"),
        "message_usage",
        "billing_status IN ('charged', 'platform_funded')",
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_billing_mode"),
        "message_usage",
        "billing_mode IN ('customer', 'platform')",
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_funding_consistency"),
        "message_usage",
        "(billing_mode = 'customer' AND billing_account_id IS NOT NULL "
        "AND pricing_rule_id IS NOT NULL AND currency IS NOT NULL "
        "AND customer_price_minor > 0 AND billing_status = 'charged') OR "
        "(billing_mode = 'platform' AND pricing_rule_id IS NULL "
        "AND currency IS NULL AND customer_price_minor = 0 "
        "AND billing_status = 'platform_funded')",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM message_usage WHERE billing_mode = 'platform') "
        "THEN RAISE EXCEPTION 'cannot downgrade while platform-funded usage exists'; "
        "END IF; END $$"
    )
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_funding_consistency"),
        "message_usage",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_billing_mode"),
        "message_usage",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_status"),
        "message_usage",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_nonnegative_price"),
        "message_usage",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_positive_price"),
        "message_usage",
        "customer_price_minor > 0",
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_status"),
        "message_usage",
        "billing_status IN ('charged')",
    )
    op.alter_column("message_usage", "currency", nullable=False)
    op.alter_column("message_usage", "pricing_rule_id", nullable=False)
    op.alter_column("message_usage", "billing_account_id", nullable=False)
    op.drop_column("message_usage", "billing_mode")
    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_billing_mode"),
        "outbound_messages",
        type_="check",
    )
    op.drop_column("outbound_messages", "billing_mode")
    op.drop_constraint(
        op.f("ck_message_templates_message_template_billing_mode"),
        "message_templates",
        type_="check",
    )
    op.drop_column("message_templates", "billing_mode")
