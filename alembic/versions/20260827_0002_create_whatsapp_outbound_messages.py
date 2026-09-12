"""Create WhatsApp outbound messages table.

Revision ID: 20260827_0002
Revises: 20260827_0001
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260827_0002"
down_revision: str | Sequence[str] | None = "20260827_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_outbound_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inbound_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("recipient_wa_id", sa.String(length=64), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("meta_message_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'uncertain')",
            name="whatsapp_outbound_message_status",
        ),
        sa.ForeignKeyConstraint(
            ["inbound_message_id"],
            ["whatsapp_inbound_messages.id"],
            name=op.f("fk_whatsapp_outbound_messages_inbound_message_id_whatsapp_inbound_messages"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_outbound_messages")),
        sa.UniqueConstraint(
            "inbound_message_id",
            "purpose",
            name="uq_whatsapp_outbound_messages_inbound_purpose",
        ),
        sa.UniqueConstraint(
            "meta_message_id",
            name="uq_whatsapp_outbound_messages_meta_message_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_outbound_messages")
