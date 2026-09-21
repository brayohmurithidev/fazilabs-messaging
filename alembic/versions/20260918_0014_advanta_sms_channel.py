"""Add Advanta SMS channel and immutable SMS accounting snapshots.

Revision ID: 20260918_0014
Revises: 20260918_0013
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260918_0014"
down_revision: str | Sequence[str] | None = "20260918_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_outbound_messages_provider_message_id", "outbound_messages", type_="unique"
    )
    op.create_unique_constraint(
        "uq_outbound_provider_message", "outbound_messages", ["provider", "provider_message_id"]
    )
    op.add_column("message_templates", sa.Column("sms_body", sa.Text(), nullable=True))
    op.add_column("message_templates", sa.Column("provider_route", sa.String(24), nullable=True))
    op.alter_column("message_templates", "provider_template_name", nullable=True)
    op.alter_column("message_templates", "language_code", nullable=True)
    op.drop_constraint(
        op.f("ck_message_templates_message_template_channel"), "message_templates", type_="check"
    )
    op.drop_constraint(
        op.f("ck_message_templates_message_template_provider"), "message_templates", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_channel"),
        "message_templates",
        "channel IN ('whatsapp', 'sms')",
    )
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_provider"),
        "message_templates",
        "provider IN ('meta', 'advanta')",
    )
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_channel_configuration"),
        "message_templates",
        "(channel = 'whatsapp' AND provider = 'meta' AND provider_template_name IS NOT NULL "
        "AND language_code IS NOT NULL AND sms_body IS NULL AND provider_route IS NULL) OR "
        "(channel = 'sms' AND provider = 'advanta' AND provider_template_name IS NULL "
        "AND language_code IS NULL AND sms_body IS NOT NULL "
        "AND provider_route IN ('standard', 'transactional'))",
    )

    op.add_column("outbound_messages", sa.Column("provider_route", sa.String(24), nullable=True))
    op.add_column(
        "outbound_messages", sa.Column("sms_character_count", sa.Integer(), nullable=True)
    )
    op.add_column("outbound_messages", sa.Column("sms_page_count", sa.Integer(), nullable=True))
    op.add_column(
        "outbound_messages", sa.Column("provider_cost_minor", sa.BigInteger(), nullable=True)
    )
    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_channel"), "outbound_messages", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_channel"),
        "outbound_messages",
        "channel IN ('whatsapp', 'sms')",
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_sms_pages"),
        "outbound_messages",
        "(channel = 'whatsapp' AND sms_character_count IS NULL "
        "AND sms_page_count IS NULL AND provider_route IS NULL) OR "
        "(channel = 'sms' AND sms_character_count BETWEEN 1 AND 960 "
        "AND sms_page_count BETWEEN 1 AND 6 "
        "AND provider_route IN ('standard', 'transactional'))",
    )

    op.drop_constraint(
        op.f("ck_pricing_rules_pricing_rule_channel"), "pricing_rules", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_pricing_rules_pricing_rule_channel"),
        "pricing_rules",
        "channel IN ('whatsapp', 'sms')",
    )
    op.add_column("message_usage", sa.Column("sms_page_count", sa.Integer(), nullable=True))
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_channel"), "message_usage", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_channel"),
        "message_usage",
        "channel IN ('whatsapp', 'sms')",
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_sms_pages"),
        "message_usage",
        "(channel = 'whatsapp' AND sms_page_count IS NULL) OR "
        "(channel = 'sms' AND sms_page_count BETWEEN 1 AND 6)",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS "
        "(SELECT 1 FROM outbound_messages WHERE channel = 'sms') OR EXISTS "
        "(SELECT 1 FROM message_templates WHERE channel = 'sms') THEN "
        "RAISE EXCEPTION 'cannot downgrade while SMS records exist'; END IF; END $$"
    )
    op.drop_constraint("uq_outbound_provider_message", "outbound_messages", type_="unique")
    op.create_unique_constraint(
        "uq_outbound_messages_provider_message_id", "outbound_messages", ["provider_message_id"]
    )
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_sms_pages"), "message_usage", type_="check"
    )
    op.drop_constraint(
        op.f("ck_message_usage_message_usage_channel"), "message_usage", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_message_usage_message_usage_channel"), "message_usage", "channel IN ('whatsapp')"
    )
    op.drop_column("message_usage", "sms_page_count")
    op.drop_constraint(
        op.f("ck_pricing_rules_pricing_rule_channel"), "pricing_rules", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_pricing_rules_pricing_rule_channel"), "pricing_rules", "channel IN ('whatsapp')"
    )
    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_sms_pages"), "outbound_messages", type_="check"
    )
    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_channel"), "outbound_messages", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_channel"),
        "outbound_messages",
        "channel IN ('whatsapp')",
    )
    op.drop_column("outbound_messages", "provider_cost_minor")
    op.drop_column("outbound_messages", "sms_page_count")
    op.drop_column("outbound_messages", "sms_character_count")
    op.drop_column("outbound_messages", "provider_route")
    op.drop_constraint(
        op.f("ck_message_templates_message_template_channel_configuration"),
        "message_templates",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_message_templates_message_template_provider"), "message_templates", type_="check"
    )
    op.drop_constraint(
        op.f("ck_message_templates_message_template_channel"), "message_templates", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_provider"),
        "message_templates",
        "provider IN ('meta')",
    )
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_channel"),
        "message_templates",
        "channel IN ('whatsapp')",
    )
    op.alter_column("message_templates", "language_code", nullable=False)
    op.alter_column("message_templates", "provider_template_name", nullable=False)
    op.drop_column("message_templates", "provider_route")
    op.drop_column("message_templates", "sms_body")
