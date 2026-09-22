"""Add a durable submitting lifecycle state and claim/lease fields.

Revision ID: 20260922_0015
Revises: 20260918_0014
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260922_0015"
down_revision: str | Sequence[str] | None = "20260918_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbound_messages", sa.Column("claimed_by", sa.String(160), nullable=True))
    op.add_column(
        "outbound_messages", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "outbound_messages",
        sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_status"), "outbound_messages", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_status"),
        "outbound_messages",
        "status IN ('pending', 'submitting', 'sent', 'delivered', 'read', 'failed', 'uncertain')",
    )

    op.create_index(
        "ix_outbound_messages_pending_created_at",
        "outbound_messages",
        ["created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_outbound_messages_submitting_lease_expiry",
        "outbound_messages",
        ["claim_lease_expires_at"],
        postgresql_where=sa.text("status = 'submitting'"),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS "
        "(SELECT 1 FROM outbound_messages WHERE status = 'submitting') THEN "
        "RAISE EXCEPTION 'cannot downgrade while submitting rows exist; "
        "resolve them to sent/failed/uncertain first'; END IF; END $$"
    )
    op.drop_index("ix_outbound_messages_submitting_lease_expiry", table_name="outbound_messages")
    op.drop_index("ix_outbound_messages_pending_created_at", table_name="outbound_messages")

    op.drop_constraint(
        op.f("ck_outbound_messages_outbound_message_status"), "outbound_messages", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_outbound_messages_outbound_message_status"),
        "outbound_messages",
        "status IN ('pending', 'sent', 'delivered', 'read', 'failed', 'uncertain')",
    )

    op.drop_column("outbound_messages", "claim_lease_expires_at")
    op.drop_column("outbound_messages", "claimed_at")
    op.drop_column("outbound_messages", "claimed_by")
