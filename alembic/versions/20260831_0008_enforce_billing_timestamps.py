"""Enforce required billing audit timestamps.

Revision ID: 20260831_0008
Revises: 20260831_0007
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_0008"
down_revision: str | Sequence[str] | None = "20260831_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table, columns in (
        ("billing_accounts", ("created_at", "updated_at")),
        ("billing_reservations", ("created_at",)),
        ("message_usage", ("created_at",)),
        ("pricing_rules", ("created_at", "updated_at")),
        ("wallet_transactions", ("created_at",)),
    ):
        for column in columns:
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )


def downgrade() -> None:
    for table, columns in (
        ("billing_accounts", ("created_at", "updated_at")),
        ("billing_reservations", ("created_at",)),
        ("message_usage", ("created_at",)),
        ("pricing_rules", ("created_at", "updated_at")),
        ("wallet_transactions", ("created_at",)),
    ):
        for column in columns:
            op.alter_column(
                table,
                column,
                existing_type=sa.DateTime(timezone=True),
                nullable=True,
            )
