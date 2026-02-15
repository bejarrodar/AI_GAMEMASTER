"""add campaign memory summaries table

Revision ID: 20260215_0003
Revises: 20260215_0002
Create Date: 2026-02-15 07:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260215_0003"
down_revision = "20260215_0002"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return inspector.has_table(table_name)


def upgrade() -> None:
    if _has_table("campaign_memory_summaries"):
        return
    op.create_table(
        "campaign_memory_summaries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_turn_id", sa.Integer(), nullable=False),
        sa.Column("end_turn_id", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_campaign_memory_summaries_campaign_id", "campaign_memory_summaries", ["campaign_id"])
    op.create_index("ix_campaign_memory_summaries_start_turn_id", "campaign_memory_summaries", ["start_turn_id"])
    op.create_index("ix_campaign_memory_summaries_end_turn_id", "campaign_memory_summaries", ["end_turn_id"])


def downgrade() -> None:
    if not _has_table("campaign_memory_summaries"):
        return
    op.drop_index("ix_campaign_memory_summaries_end_turn_id", table_name="campaign_memory_summaries")
    op.drop_index("ix_campaign_memory_summaries_start_turn_id", table_name="campaign_memory_summaries")
    op.drop_index("ix_campaign_memory_summaries_campaign_id", table_name="campaign_memory_summaries")
    op.drop_table("campaign_memory_summaries")

