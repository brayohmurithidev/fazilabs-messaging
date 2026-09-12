"""Add uncertain-message reconciliation audit and billing exceptions.

Revision ID: 20260831_0009
Revises: 20260831_0008
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260831_0009"
down_revision: str | Sequence[str] | None = "20260831_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbound_messages", sa.Column("reconciliation_status", sa.String(32)))
    op.create_table(
        "message_reconciliation_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("provider_message_id", sa.String(255)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('resolved_accepted', 'resolved_rejected', 'unresolved', 'released')",
            name=op.f("ck_message_reconciliation_attempts_message_reconciliation_attempt_status"),
        ),
        sa.CheckConstraint(
            "method IN ('operator', 'webhook')",
            name=op.f("ck_message_reconciliation_attempts_message_reconciliation_attempt_method"),
        ),
        sa.ForeignKeyConstraint(
            ["outbound_message_id"], ["outbound_messages.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_message_reconciliation_attempts_outbound_message_id"),
        "message_reconciliation_attempts",
        ["outbound_message_id"],
    )
    op.create_table(
        "billing_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("billing_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "type IN ('late_provider_acceptance_after_release')",
            name=op.f("ck_billing_exceptions_billing_exception_type"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name=op.f("ck_billing_exceptions_billing_exception_status"),
        ),
        sa.CheckConstraint(
            "amount_minor > 0",
            name=op.f("ck_billing_exceptions_billing_exception_positive_amount"),
        ),
        sa.ForeignKeyConstraint(
            ["billing_account_id"], ["billing_accounts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["outbound_message_id"], ["outbound_messages.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "outbound_message_id", "type", name="uq_billing_exception_outbound_type"
        ),
    )
    op.create_index(
        op.f("ix_billing_exceptions_billing_account_id"),
        "billing_exceptions",
        ["billing_account_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_billing_exceptions_billing_account_id"), table_name="billing_exceptions")
    op.drop_table("billing_exceptions")
    op.drop_index(
        op.f("ix_message_reconciliation_attempts_outbound_message_id"),
        table_name="message_reconciliation_attempts",
    )
    op.drop_table("message_reconciliation_attempts")
    op.drop_column("outbound_messages", "reconciliation_status")
