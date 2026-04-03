"""add campaign version column for optimistic locking

Revision ID: 20260215_0001
Revises:
Create Date: 2026-02-15 05:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260215_0001"
down_revision = None
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _has_column("campaigns", "version"):
        with op.batch_alter_table("campaigns") as batch_op:
            batch_op.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        op.execute("UPDATE campaigns SET version = 1 WHERE version IS NULL")
        with op.batch_alter_table("campaigns") as batch_op:
            batch_op.alter_column("version", server_default=None)


def downgrade() -> None:
    if _has_column("campaigns", "version"):
        with op.batch_alter_table("campaigns") as batch_op:
            batch_op.drop_column("version")

