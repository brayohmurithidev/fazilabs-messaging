"""Pivot outbound transport records into the Fazilabs Messaging Platform.

Revision ID: 20260828_0004
Revises: 20260828_0003
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260828_0004"
down_revision: str | Sequence[str] | None = "20260828_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "messaging_applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name=op.f("ck_messaging_applications_messaging_application_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messaging_applications")),
        sa.UniqueConstraint("slug", name=op.f("uq_messaging_applications_slug")),
    )
    op.create_table(
        "messaging_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name=op.f("ck_messaging_api_keys_messaging_api_key_status"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["messaging_applications.id"],
            name=op.f("fk_messaging_api_keys_application_id_messaging_applications"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messaging_api_keys")),
        sa.UniqueConstraint("key_prefix", name=op.f("uq_messaging_api_keys_key_prefix")),
    )
    op.create_index(
        op.f("ix_messaging_api_keys_application_id"), "messaging_api_keys", ["application_id"]
    )
    op.create_table(
        "message_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_key", sa.String(100), nullable=False),
        sa.Column("channel", sa.String(24), nullable=False),
        sa.Column("provider_template_name", sa.String(512), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('whatsapp')", name=op.f("ck_message_templates_message_template_channel")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name=op.f("ck_message_templates_message_template_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_templates")),
        sa.UniqueConstraint("template_key", "channel", name="uq_message_templates_key_channel"),
    )
    op.rename_table("whatsapp_outbound_messages", "outbound_messages")
    # PostgreSQL retains old constraint names when a table is renamed.
    op.execute(
        "ALTER TABLE outbound_messages DROP CONSTRAINT "
        "ck_whatsapp_outbound_messages_whatsapp_outbound_message_status"
    )
    op.execute(
        "ALTER TABLE outbound_messages DROP CONSTRAINT "
        "uq_whatsapp_outbound_messages_inbound_purpose"
    )
    op.alter_column(
        "outbound_messages",
        "recipient_wa_id",
        new_column_name="recipient",
        existing_type=sa.String(64),
        type_=sa.String(32),
        existing_nullable=False,
    )
    op.alter_column(
        "outbound_messages",
        "message_type",
        new_column_name="message_kind",
        existing_type=sa.String(32),
        type_=sa.String(24),
        existing_nullable=False,
    )
    op.alter_column(
        "outbound_messages",
        "meta_message_id",
        new_column_name="provider_message_id",
        existing_type=sa.String(255),
        existing_nullable=True,
    )
    op.alter_column(
        "outbound_messages",
        "status",
        existing_type=sa.String(9),
        type_=sa.String(16),
        existing_nullable=False,
    )
    op.alter_column("outbound_messages", "text_body", existing_type=sa.Text(), nullable=True)
    op.add_column(
        "outbound_messages",
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "outbound_messages",
        sa.Column("channel", sa.String(24), server_default="whatsapp", nullable=False),
    )
    op.add_column("outbound_messages", sa.Column("template_name", sa.String(100), nullable=True))
    op.add_column(
        "outbound_messages", sa.Column("template_parameters", postgresql.JSONB(), nullable=True)
    )
    op.add_column("outbound_messages", sa.Column("source_type", sa.String(80), nullable=True))
    op.add_column("outbound_messages", sa.Column("source_id", sa.String(160), nullable=True))
    op.add_column("outbound_messages", sa.Column("metadata", postgresql.JSONB(), nullable=True))
    op.add_column("outbound_messages", sa.Column("idempotency_key", sa.String(200), nullable=True))
    op.add_column("outbound_messages", sa.Column("payload_hash", sa.String(64), nullable=True))
    op.add_column(
        "outbound_messages", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "outbound_messages", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "outbound_messages", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute(
        "UPDATE outbound_messages SET provider = 'meta' "
        "WHERE provider IS NULL OR provider = 'openai'"
    )
    op.alter_column("outbound_messages", "provider", existing_type=sa.String(64), nullable=False)
    op.drop_column("outbound_messages", "purpose")
    op.drop_column("outbound_messages", "provider_model")
    op.drop_column("outbound_messages", "provider_response_id")
    op.create_foreign_key(
        op.f("fk_outbound_messages_application_id_messaging_applications"),
        "outbound_messages",
        "messaging_applications",
        ["application_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_outbound_app_idempotency", "outbound_messages", ["application_id", "idempotency_key"]
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_status"),
        "outbound_messages",
        "status IN ('pending', 'sent', 'delivered', 'read', 'failed', 'uncertain')",
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_channel"),
        "outbound_messages",
        "channel IN ('whatsapp')",
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_kind"),
        "outbound_messages",
        "message_kind IN ('text', 'template')",
    )
    op.alter_column("outbound_messages", "channel", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_kind"), "outbound_messages", type_="check"
    )
    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_channel"), "outbound_messages", type_="check"
    )
    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_status"), "outbound_messages", type_="check"
    )
    op.drop_constraint("uq_outbound_app_idempotency", "outbound_messages", type_="unique")
    op.drop_constraint(
        op.f("fk_outbound_messages_application_id_messaging_applications"),
        "outbound_messages",
        type_="foreignkey",
    )
    op.add_column(
        "outbound_messages", sa.Column("provider_response_id", sa.String(255), nullable=True)
    )
    op.add_column("outbound_messages", sa.Column("provider_model", sa.String(128), nullable=True))
    op.add_column(
        "outbound_messages",
        sa.Column("purpose", sa.String(32), server_default="legacy", nullable=False),
    )
    for column in (
        "failed_at",
        "read_at",
        "delivered_at",
        "payload_hash",
        "idempotency_key",
        "metadata",
        "source_id",
        "source_type",
        "template_parameters",
        "template_name",
        "channel",
        "application_id",
    ):
        op.drop_column("outbound_messages", column)
    op.alter_column("outbound_messages", "text_body", existing_type=sa.Text(), nullable=False)
    op.alter_column(
        "outbound_messages",
        "status",
        existing_type=sa.String(16),
        type_=sa.String(9),
        existing_nullable=False,
    )
    op.alter_column(
        "outbound_messages",
        "provider_message_id",
        new_column_name="meta_message_id",
        existing_type=sa.String(255),
        existing_nullable=True,
    )
    op.alter_column(
        "outbound_messages",
        "message_kind",
        new_column_name="message_type",
        existing_type=sa.String(24),
        type_=sa.String(32),
        existing_nullable=False,
    )
    op.alter_column(
        "outbound_messages",
        "recipient",
        new_column_name="recipient_wa_id",
        existing_type=sa.String(32),
        type_=sa.String(64),
        existing_nullable=False,
    )
    op.create_unique_constraint(
        "uq_whatsapp_outbound_messages_inbound_purpose",
        "outbound_messages",
        ["inbound_message_id", "purpose"],
    )
    op.create_check_constraint(
        "ck_whatsapp_outbound_messages_whatsapp_outbound_message_status",
        "outbound_messages",
        "status IN ('pending', 'sent', 'failed', 'uncertain')",
    )
    op.rename_table("outbound_messages", "whatsapp_outbound_messages")
    op.drop_table("message_templates")
    op.drop_index(op.f("ix_messaging_api_keys_application_id"), table_name="messaging_api_keys")
    op.drop_table("messaging_api_keys")
    op.drop_table("messaging_applications")
