from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy import event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aigm.db.base import Base


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_thread_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="dnd")
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    __mapper_args__ = {"version_id_col": version}

    turns: Mapped[list[TurnLog]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    players: Mapped[list[Player]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    characters: Mapped[list[Character]] = relationship(back_populates="campaign", cascade="all, delete-orphan")
    rules: Mapped[list[CampaignRule]] = relationship(back_populates="campaign", cascade="all, delete-orphan")


class TurnLog(Base):
    __tablename__ = "turn_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    user_input: Mapped[str] = mapped_column(Text, nullable=False)
    ai_raw_output: Mapped[str] = mapped_column(Text, nullable=False)
    accepted_commands: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    rejected_commands: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    narration: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="turns")


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    discord_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ProcessedDiscordMessage(Base):
    __tablename__ = "processed_discord_messages"
    __table_args__ = (UniqueConstraint("discord_message_id", name="uq_processed_discord_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    discord_message_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    actor_discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class Player(Base):
    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("campaign_id", "discord_user_id", name="uq_player_campaign_discord_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="players")
    inventory_items: Mapped[list[InventoryItem]] = relationship(back_populates="player", cascade="all, delete-orphan")


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    player_id: Mapped[int | None] = mapped_column(ForeignKey("players.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="adventurer")
    hp: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_hp: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    item_states: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    effects: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="characters")


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (UniqueConstraint("player_id", "item_key", name="uq_inventory_player_item"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), nullable=False)
    item_key: Mapped[str] = mapped_column(String(128), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)

    player: Mapped[Player] = relationship(back_populates="inventory_items")


class CampaignRule(Base):
    __tablename__ = "campaign_rules"
    __table_args__ = (UniqueConstraint("campaign_id", "rule_key", name="uq_campaign_rule_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    rule_key: Mapped[str] = mapped_column(String(128), nullable=False)
    rule_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="rules")


class AgencyRuleBlock(Base):
    __tablename__ = "agency_rule_blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    priority: Mapped[str] = mapped_column(String(32), nullable=False, default="high")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SysAdminUser(Base):
    __tablename__ = "sys_admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class LearnedRelevance(Base):
    __tablename__ = "learned_relevance"
    __table_args__ = (UniqueConstraint("campaign_id", "item_key", "context_tag", name="uq_learned_relevance_triplet"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    item_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    context_tag: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    interaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class GlobalLearnedRelevance(Base):
    __tablename__ = "global_learned_relevance"
    __table_args__ = (UniqueConstraint("item_key", "context_tag", name="uq_global_learned_relevance_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    context_tag: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    interaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ItemKnowledge(Base):
    __tablename__ = "item_knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    object_type: Mapped[str] = mapped_column(String(64), nullable=False, default="unknown")
    portability: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    rarity: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ItemObservation(Base):
    __tablename__ = "item_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    turn_log_id: Mapped[int | None] = mapped_column(ForeignKey("turn_logs.id", ondelete="SET NULL"), nullable=True)
    observation_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class GlobalEffectRelevance(Base):
    __tablename__ = "global_effect_relevance"
    __table_args__ = (UniqueConstraint("effect_key", "context_tag", name="uq_global_effect_relevance_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effect_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    context_tag: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    interaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class EffectKnowledge(Base):
    __tablename__ = "effect_knowledge"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effect_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="misc")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    aliases: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    properties: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class EffectObservation(Base):
    __tablename__ = "effect_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effect_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    turn_log_id: Mapped[int | None] = mapped_column(ForeignKey("turn_logs.id", ondelete="SET NULL"), nullable=True)
    observation_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class CampaignSnapshot(Base):
    __tablename__ = "campaign_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_key", name="uq_campaign_snapshot_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False, default="all")
    snapshot_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class CampaignMemorySummary(Base):
    __tablename__ = "campaign_memory_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    start_turn_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    end_turn_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class GameRuleset(Base):
    __tablename__ = "game_rulesets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    system: Mapped[str] = mapped_column(String(64), nullable=False, default="dnd")
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rules_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Rulebook(Base):
    __tablename__ = "rulebooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    system: Mapped[str] = mapped_column(String(64), nullable=False, default="dnd")
    version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RulebookEntry(Base):
    __tablename__ = "rulebook_entries"
    __table_args__ = (UniqueConstraint("rulebook_id", "entry_key", name="uq_rulebook_entry_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rulebook_id: Mapped[int] = mapped_column(ForeignKey("rulebooks.id", ondelete="CASCADE"), nullable=False, index=True)
    entry_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    section: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    page_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    searchable_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class DiceRollLog(Base):
    __tablename__ = "dice_roll_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    actor_display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    expression: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    normalized_expression: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    sides: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    roll_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    modifier: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    advantage_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class AuthUser(Base):
    __tablename__ = "auth_users"
    __table_args__ = (UniqueConstraint("username", name="uq_auth_user_username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    password_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    discord_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuthRole(Base):
    __tablename__ = "auth_roles"
    __table_args__ = (UniqueConstraint("name", name="uq_auth_role_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuthPermission(Base):
    __tablename__ = "auth_permissions"
    __table_args__ = (UniqueConstraint("name", name="uq_auth_permission_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuthUserRole(Base):
    __tablename__ = "auth_user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_auth_user_role_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("auth_roles.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class AuthRolePermission(Base):
    __tablename__ = "auth_role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "permission_id", name="uq_auth_role_permission_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("auth_roles.id", ondelete="CASCADE"), nullable=False, index=True)
    permission_id: Mapped[int] = mapped_column(
        ForeignKey("auth_permissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(32), nullable=False, default="INFO", index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="runtime")
    log_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    actor_display: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    audit_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


@event.listens_for(AdminAuditLog, "before_update")
def _prevent_admin_audit_update(_mapper, _connection, _target) -> None:
    raise ValueError("admin_audit_logs is append-only and cannot be updated.")


@event.listens_for(AdminAuditLog, "before_delete")
def _prevent_admin_audit_delete(_mapper, _connection, _target) -> None:
    raise ValueError("admin_audit_logs is append-only and cannot be deleted.")


class BotConfig(Base):
    __tablename__ = "bot_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    discord_token: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
