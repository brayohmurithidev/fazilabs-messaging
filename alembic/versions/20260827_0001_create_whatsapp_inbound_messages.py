"""Create WhatsApp inbound messages table.

Revision ID: 20260827_0001
Revises:
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260827_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_inbound_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meta_message_id", sa.String(length=255), nullable=False),
        sa.Column("sender_wa_id", sa.String(length=64), nullable=False),
        sa.Column("recipient_phone_number_id", sa.String(length=64), nullable=False),
        sa.Column("message_type", sa.String(length=64), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=True),
        sa.Column("message_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_whatsapp_inbound_messages")),
        sa.UniqueConstraint("meta_message_id", name="uq_whatsapp_inbound_messages_meta_message_id"),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_inbound_messages")
