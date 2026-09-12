"""Add application-owned production template mappings.

Revision ID: 20260829_0006
Revises: 20260828_0005
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260829_0006"
down_revision: str | Sequence[str] | None = "20260828_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_message_templates_key_channel", "message_templates", type_="unique")
    op.add_column(
        "message_templates",
        sa.Column("application_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "message_templates",
        sa.Column("provider", sa.String(32), server_default="meta", nullable=False),
    )
    op.add_column("message_templates", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "message_templates", sa.Column("parameter_schema", postgresql.JSONB(), nullable=True)
    )
    op.alter_column(
        "message_templates",
        "language",
        new_column_name="language_code",
        existing_type=sa.String(16),
        existing_nullable=False,
    )
    op.drop_column("message_templates", "category")
    existing_count = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM message_templates")).scalar()
    )
    if existing_count:
        raise RuntimeError(
            "Existing global message templates must be assigned to applications before upgrade"
        )
    op.alter_column(
        "message_templates",
        "application_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        op.f("fk_message_templates_application_id_messaging_applications"),
        "message_templates",
        "messaging_applications",
        ["application_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        op.f("ix_message_templates_application_id"),
        "message_templates",
        ["application_id"],
    )
    op.create_unique_constraint(
        "uq_message_templates_application_key_channel",
        "message_templates",
        ["application_id", "template_key", "channel"],
    )
    op.create_check_constraint(
        op.f("ck_message_templates_message_template_provider"),
        "message_templates",
        "provider IN ('meta')",
    )
    op.alter_column("message_templates", "provider", server_default=None)
    op.add_column(
        "outbound_messages", sa.Column("provider_template_name", sa.String(512), nullable=True)
    )
    op.add_column(
        "outbound_messages", sa.Column("template_language_code", sa.String(16), nullable=True)
    )


def downgrade() -> None:
    template_count = (
        op.get_bind().execute(sa.text("SELECT count(*) FROM message_templates")).scalar()
    )
    if template_count:
        raise RuntimeError("Refusing to discard application template ownership during downgrade")
    op.drop_column("outbound_messages", "template_language_code")
    op.drop_column("outbound_messages", "provider_template_name")
    op.drop_constraint(
        op.f("ck_message_templates_message_template_provider"),
        "message_templates",
        type_="check",
    )
    op.drop_constraint(
        "uq_message_templates_application_key_channel", "message_templates", type_="unique"
    )
    op.drop_index(op.f("ix_message_templates_application_id"), table_name="message_templates")
    op.drop_constraint(
        op.f("fk_message_templates_application_id_messaging_applications"),
        "message_templates",
        type_="foreignkey",
    )
    op.alter_column(
        "message_templates",
        "language_code",
        new_column_name="language",
        existing_type=sa.String(16),
        existing_nullable=False,
    )
    op.add_column("message_templates", sa.Column("category", sa.String(32), nullable=True))
    op.drop_column("message_templates", "parameter_schema")
    op.drop_column("message_templates", "description")
    op.drop_column("message_templates", "provider")
    op.drop_column("message_templates", "application_id")
    op.create_unique_constraint(
        "uq_message_templates_key_channel", "message_templates", ["template_key", "channel"]
    )
