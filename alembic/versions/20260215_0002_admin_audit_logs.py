"""add admin audit logs table

Revision ID: 20260215_0002
Revises: 20260215_0001
Create Date: 2026-02-15 05:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260215_0002"
down_revision = "20260215_0001"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return inspector.has_table(table_name)


def upgrade() -> None:
    if _has_table("admin_audit_logs"):
        return
    op.create_table(
        "admin_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor_source", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("actor_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("actor_display", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_admin_audit_logs_actor_source", "admin_audit_logs", ["actor_source"])
    op.create_index("ix_admin_audit_logs_actor_id", "admin_audit_logs", ["actor_id"])
    op.create_index("ix_admin_audit_logs_action", "admin_audit_logs", ["action"])
    op.create_index("ix_admin_audit_logs_created_at", "admin_audit_logs", ["created_at"])


def downgrade() -> None:
    if not _has_table("admin_audit_logs"):
        return
    op.drop_index("ix_admin_audit_logs_created_at", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_action", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_actor_id", table_name="admin_audit_logs")
    op.drop_index("ix_admin_audit_logs_actor_source", table_name="admin_audit_logs")
    op.drop_table("admin_audit_logs")

