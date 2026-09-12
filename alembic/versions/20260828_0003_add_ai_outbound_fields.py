"""Add AI response fields to outbound messages.

Revision ID: 20260828_0003
Revises: 20260827_0002
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260828_0003"
down_revision: str | Sequence[str] | None = "20260827_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_outbound_messages", sa.Column("provider", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "whatsapp_outbound_messages",
        sa.Column("provider_model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "whatsapp_outbound_messages",
        sa.Column("provider_response_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("whatsapp_outbound_messages", "provider_response_id")
    op.drop_column("whatsapp_outbound_messages", "provider_model")
    op.drop_column("whatsapp_outbound_messages", "provider")
