"""Add application billing enforcement policy.

Revision ID: 20260912_0012
Revises: 20260831_0011
Create Date: 2026-09-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260912_0012"
down_revision: str | Sequence[str] | None = "20260831_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messaging_applications",
        sa.Column("billing_required", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("messaging_applications", "billing_required")
