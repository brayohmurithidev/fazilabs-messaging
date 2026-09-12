"""Align the renamed provider message uniqueness constraint.

Revision ID: 20260828_0005
Revises: 20260828_0004
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0005"
down_revision: str | Sequence[str] | None = "20260828_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE outbound_messages RENAME CONSTRAINT "
        "uq_whatsapp_outbound_messages_meta_message_id TO "
        "uq_outbound_messages_provider_message_id"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE outbound_messages RENAME CONSTRAINT "
        "uq_outbound_messages_provider_message_id TO "
        "uq_whatsapp_outbound_messages_meta_message_id"
    )
