"""Add explicit billing exception resolution lifecycle.

Revision ID: 20260831_0011
Revises: 20260831_0010
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_0011"
down_revision: str | Sequence[str] | None = "20260831_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("billing_exceptions", sa.Column("resolution_reason", sa.Text(), nullable=True))
    op.drop_constraint(
        op.f("ck_billing_exceptions_billing_exception_status"),
        "billing_exceptions",
        type_="check",
    )
    op.execute(
        "UPDATE billing_exceptions SET status = 'resolved_waived', "
        "resolution_reason = COALESCE(resolution_reason, 'legacy_resolution_without_charge'), "
        "resolved_at = COALESCE(resolved_at, created_at) "
        "WHERE status = 'resolved'"
    )
    op.create_check_constraint(
        op.f("ck_billing_exceptions_billing_exception_status"),
        "billing_exceptions",
        "status IN ('open', 'resolved_charged', 'resolved_waived')",
    )
    op.create_check_constraint(
        op.f("ck_billing_exceptions_billing_exception_resolution_fields"),
        "billing_exceptions",
        "(status = 'open' AND resolved_at IS NULL AND resolution_reason IS NULL) OR "
        "(status != 'open' AND resolved_at IS NOT NULL AND resolution_reason IS NOT NULL)",
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE billing_exceptions DROP CONSTRAINT IF EXISTS "
        "ck_billing_exceptions_billing_exception_resolution_fields"
    )
    op.drop_constraint(
        op.f("ck_billing_exceptions_billing_exception_status"),
        "billing_exceptions",
        type_="check",
    )
    op.execute("UPDATE billing_exceptions SET status = 'resolved' WHERE status != 'open'")
    op.create_check_constraint(
        op.f("ck_billing_exceptions_billing_exception_status"),
        "billing_exceptions",
        "status IN ('open', 'resolved')",
    )
    op.drop_column("billing_exceptions", "resolution_reason")
