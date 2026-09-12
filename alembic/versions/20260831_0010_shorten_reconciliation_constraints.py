"""Use stable short reconciliation constraint names.

Revision ID: 20260831_0010
Revises: 20260831_0009
Create Date: 2026-08-31
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260831_0010"
down_revision: str | Sequence[str] | None = "20260831_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE message_reconciliation_attempts DROP CONSTRAINT "
        '"ck_message_reconciliation_attempts_message_reconciliati_a9de"'
    )
    op.execute(
        "ALTER TABLE message_reconciliation_attempts DROP CONSTRAINT "
        '"ck_message_reconciliation_attempts_message_reconciliati_afc5"'
    )
    op.create_check_constraint(
        op.f("ck_message_reconciliation_attempts_recon_attempt_status"),
        "message_reconciliation_attempts",
        "status IN ('resolved_accepted', 'resolved_rejected', 'unresolved', 'released')",
    )
    op.create_check_constraint(
        op.f("ck_message_reconciliation_attempts_recon_attempt_method"),
        "message_reconciliation_attempts",
        "method IN ('operator', 'webhook')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_message_reconciliation_attempts_recon_attempt_method"),
        "message_reconciliation_attempts",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_message_reconciliation_attempts_recon_attempt_status"),
        "message_reconciliation_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_message_reconciliation_attempts_message_reconciliati_a9de",
        "message_reconciliation_attempts",
        "method IN ('operator', 'webhook')",
    )
    op.create_check_constraint(
        "ck_message_reconciliation_attempts_message_reconciliati_afc5",
        "message_reconciliation_attempts",
        "status IN ('resolved_accepted', 'resolved_rejected', 'unresolved', 'released')",
    )
