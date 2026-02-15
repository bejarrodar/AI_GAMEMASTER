"""add gameplay knowledge and dice tables

Revision ID: 20260215_0004
Revises: 20260215_0003
Create Date: 2026-02-15 09:30:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260215_0004"
down_revision = "20260215_0003"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return inspector.has_table(table_name)


def upgrade() -> None:
    if not _has_table("game_rulesets"):
        op.create_table(
            "game_rulesets",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("key", sa.String(length=64), nullable=False, unique=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("system", sa.String(length=64), nullable=False, server_default="dnd"),
            sa.Column("version", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_official", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("rules_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_game_rulesets_key", "game_rulesets", ["key"])

    if not _has_table("rulebooks"):
        op.create_table(
            "rulebooks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("slug", sa.String(length=128), nullable=False, unique=True),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("system", sa.String(length=64), nullable=False, server_default="dnd"),
            sa.Column("version", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("source", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_rulebooks_slug", "rulebooks", ["slug"])

    if not _has_table("rulebook_entries"):
        op.create_table(
            "rulebook_entries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("rulebook_id", sa.Integer(), sa.ForeignKey("rulebooks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("entry_key", sa.String(length=128), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("section", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("page_ref", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("searchable_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("rulebook_id", "entry_key", name="uq_rulebook_entry_key"),
        )
        op.create_index("ix_rulebook_entries_rulebook_id", "rulebook_entries", ["rulebook_id"])
        op.create_index("ix_rulebook_entries_entry_key", "rulebook_entries", ["entry_key"])

    if not _has_table("dice_roll_logs"):
        op.create_table(
            "dice_roll_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True),
            sa.Column("actor_discord_user_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("actor_display_name", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("expression", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("normalized_expression", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("sides", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("roll_count", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("modifier", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("advantage_mode", sa.String(length=16), nullable=False, server_default="none"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("breakdown", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_dice_roll_logs_campaign_id", "dice_roll_logs", ["campaign_id"])
        op.create_index("ix_dice_roll_logs_actor_discord_user_id", "dice_roll_logs", ["actor_discord_user_id"])


def downgrade() -> None:
    if _has_table("dice_roll_logs"):
        op.drop_index("ix_dice_roll_logs_actor_discord_user_id", table_name="dice_roll_logs")
        op.drop_index("ix_dice_roll_logs_campaign_id", table_name="dice_roll_logs")
        op.drop_table("dice_roll_logs")
    if _has_table("rulebook_entries"):
        op.drop_index("ix_rulebook_entries_entry_key", table_name="rulebook_entries")
        op.drop_index("ix_rulebook_entries_rulebook_id", table_name="rulebook_entries")
        op.drop_table("rulebook_entries")
    if _has_table("rulebooks"):
        op.drop_index("ix_rulebooks_slug", table_name="rulebooks")
        op.drop_table("rulebooks")
    if _has_table("game_rulesets"):
        op.drop_index("ix_game_rulesets_key", table_name="game_rulesets")
        op.drop_table("game_rulesets")
