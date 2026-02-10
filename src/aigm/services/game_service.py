from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from aigm.adapters.llm import LLMAdapter
from aigm.config import settings
from aigm.core.context_builder import ContextBuilder
from aigm.core.prompts import DEFAULT_RULE_BLOCKS, RULE_PROFILES, build_system_prompt, rule_ids_for_profile
from aigm.core.state_machine import apply_commands, tick_effects
from aigm.core.validator import validate_commands
from aigm.db.models import (
    AgencyRuleBlock,
    Campaign,
    CampaignRule,
    Character,
    InventoryItem,
    Player,
    SysAdminUser,
    TurnLog,
)
from aigm.schemas.game import Command, WorldState


class GameService:
    def __init__(self, llm: LLMAdapter):
        self.llm = llm
        self.context_builder = ContextBuilder()

    def get_or_create_campaign(self, db: Session, thread_id: str, mode: str = "dnd") -> Campaign:
        self.seed_default_agency_rules(db)
        campaign = db.query(Campaign).filter(Campaign.discord_thread_id == thread_id).one_or_none()
        if campaign:
            return campaign

        generated_state = self.llm.generate_world_seed(mode=mode)
        campaign = Campaign(discord_thread_id=thread_id, mode=mode, state=generated_state.model_dump())
        db.add(campaign)
        db.commit()
        db.refresh(campaign)
        return campaign

    def seed_default_agency_rules(self, db: Session) -> None:
        for rule_id, body in DEFAULT_RULE_BLOCKS.items():
            exists = db.query(AgencyRuleBlock).filter(AgencyRuleBlock.rule_id == rule_id).one_or_none()
            if exists:
                continue
            title = "Unknown"
            priority = "high"
            for line in body.splitlines():
                if line.startswith("TITLE:"):
                    title = line.removeprefix("TITLE:").strip()
                if line.startswith("PRIORITY:"):
                    priority = line.removeprefix("PRIORITY:").strip()
            db.add(
                AgencyRuleBlock(rule_id=rule_id, title=title, priority=priority, body=body, is_enabled=True)
            )
        db.commit()

    def ensure_player(self, db: Session, campaign: Campaign, actor_id: str, display_name: str) -> Player:
        player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor_id)
            .one_or_none()
        )
        if player:
            if player.display_name != display_name:
                player.display_name = display_name
                db.add(player)
                db.commit()
            return player

        player = Player(campaign_id=campaign.id, discord_user_id=actor_id, display_name=display_name)
        db.add(player)
        db.commit()
        db.refresh(player)
        return player

    def player_character_name(self, db: Session, campaign_id: int, player_id: int) -> str | None:
        row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign_id, Character.player_id == player_id)
            .one_or_none()
        )
        return row.name if row else None

    def register_character_from_description(
        self,
        db: Session,
        campaign: Campaign,
        player: Player,
        description: str,
    ) -> str:
        current_state = WorldState.model_validate(campaign.state)
        suggested = self.llm.generate_character_from_description(description, fallback_name=player.display_name)

        unique_name = suggested.name
        idx = 2
        while unique_name in current_state.party:
            unique_name = f"{suggested.name}_{idx}"
            idx += 1
        suggested.name = unique_name

        current_state.party[unique_name] = suggested
        campaign.state = current_state.model_dump()
        db.add(campaign)

        char_row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign.id, Character.player_id == player.id)
            .one_or_none()
        )
        if char_row:
            char_row.name = unique_name
            char_row.hp = suggested.hp
            char_row.max_hp = suggested.max_hp
            char_row.stats = suggested.stats
            char_row.item_states = suggested.item_states
            char_row.effects = [e.model_dump() for e in suggested.effects]
            db.add(char_row)
        else:
            db.add(
                Character(
                    campaign_id=campaign.id,
                    player_id=player.id,
                    name=unique_name,
                    role="player_character",
                    hp=suggested.hp,
                    max_hp=suggested.max_hp,
                    stats=suggested.stats,
                    item_states=suggested.item_states,
                    effects=[e.model_dump() for e in suggested.effects],
                )
            )
        db.commit()
        return unique_name

    def build_campaign_system_prompt(self, db: Session, campaign: Campaign) -> str:
        rules = self.list_rules(db, campaign)
        character_instructions = rules.get("character_instructions", "")
        custom_directives = rules.get("system_prompt_custom", "")

        explicit_ids = rules.get("agency_rule_ids", "").strip()
        if explicit_ids:
            rule_ids = [rid.strip() for rid in explicit_ids.split(",") if rid.strip() in self.available_rule_ids(db)]
        else:
            profile = rules.get("agency_rule_profile", "balanced").strip().lower()
            rule_ids = rule_ids_for_profile(profile)

        db_blocks = {
            row.rule_id: row.body
            for row in db.query(AgencyRuleBlock).filter(AgencyRuleBlock.is_enabled.is_(True)).all()
        }

        return build_system_prompt(
            character_instructions=character_instructions,
            custom_directives=custom_directives,
            rule_ids=rule_ids,
            rule_blocks=db_blocks,
        )

    def available_rule_profiles(self) -> list[str]:
        return list(RULE_PROFILES.keys())

    def available_rule_ids(self, db: Session) -> list[str]:
        return [r.rule_id for r in db.query(AgencyRuleBlock).filter(AgencyRuleBlock.is_enabled.is_(True)).all()]

    def list_rules(self, db: Session, campaign: Campaign) -> dict[str, str]:
        rows = db.query(CampaignRule).filter(CampaignRule.campaign_id == campaign.id).all()
        return {r.rule_key: r.rule_value for r in rows}

    def set_rule(self, db: Session, campaign: Campaign, key: str, value: str) -> None:
        row = (
            db.query(CampaignRule)
            .filter(CampaignRule.campaign_id == campaign.id, CampaignRule.rule_key == key)
            .one_or_none()
        )
        if row:
            row.rule_value = value
            db.add(row)
        else:
            db.add(CampaignRule(campaign_id=campaign.id, rule_key=key, rule_value=value))
        db.commit()

    def authenticate_sys_admin(self, db: Session, discord_user_id: str, display_name: str, token: str) -> bool:
        if not settings.sys_admin_token or token != settings.sys_admin_token:
            return False

        admin = db.query(SysAdminUser).filter(SysAdminUser.discord_user_id == discord_user_id).one_or_none()
        if admin:
            admin.display_name = display_name
            admin.is_active = True
            admin.last_login_at = datetime.utcnow()
            db.add(admin)
        else:
            db.add(
                SysAdminUser(
                    discord_user_id=discord_user_id,
                    display_name=display_name,
                    is_active=True,
                    last_login_at=datetime.utcnow(),
                )
            )
        db.commit()
        return True

    def is_sys_admin(self, db: Session, discord_user_id: str) -> bool:
        admin = db.query(SysAdminUser).filter(SysAdminUser.discord_user_id == discord_user_id).one_or_none()
        return bool(admin and admin.is_active)

    def admin_list_rule_blocks(self, db: Session) -> list[AgencyRuleBlock]:
        return db.query(AgencyRuleBlock).order_by(AgencyRuleBlock.rule_id.asc()).all()

    def admin_upsert_rule_block(self, db: Session, rule_id: str, title: str, priority: str, body: str) -> None:
        row = db.query(AgencyRuleBlock).filter(AgencyRuleBlock.rule_id == rule_id).one_or_none()
        if row:
            row.title = title
            row.priority = priority
            row.body = body
            row.is_enabled = True
            row.updated_at = datetime.utcnow()
            db.add(row)
        else:
            db.add(
                AgencyRuleBlock(
                    rule_id=rule_id,
                    title=title,
                    priority=priority,
                    body=body,
                    is_enabled=True,
                )
            )
        db.commit()

    def admin_remove_rule_block(self, db: Session, rule_id: str) -> bool:
        row = db.query(AgencyRuleBlock).filter(AgencyRuleBlock.rule_id == rule_id).one_or_none()
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    def sync_inventory_for_player(self, db: Session, player: Player, state: WorldState) -> None:
        char_name = self.player_character_name(db, campaign_id=player.campaign_id, player_id=player.id)
        if not char_name:
            return

        char = state.party.get(char_name)
        if not char:
            return

        existing_rows = db.query(InventoryItem).filter(InventoryItem.player_id == player.id).all()
        existing_map = {r.item_key: r for r in existing_rows}
        state_keys = set(char.inventory.keys())

        for item_key, quantity in char.inventory.items():
            row = existing_map.get(item_key)
            if row:
                row.quantity = quantity
                db.add(row)
            else:
                db.add(InventoryItem(player_id=player.id, item_key=item_key, quantity=quantity, metadata={}))

        for item_key, row in existing_map.items():
            if item_key not in state_keys:
                db.delete(row)

        db.commit()

    def sync_character_row(self, db: Session, campaign: Campaign, player: Player, state: WorldState) -> None:
        char_name = self.player_character_name(db, campaign_id=campaign.id, player_id=player.id)
        if not char_name or char_name not in state.party:
            return

        char_state = state.party[char_name]
        row = (
            db.query(Character)
            .filter(Character.campaign_id == campaign.id, Character.player_id == player.id)
            .one_or_none()
        )
        if not row:
            return

        row.name = char_state.name
        row.hp = char_state.hp
        row.max_hp = char_state.max_hp
        row.stats = char_state.stats
        row.item_states = char_state.item_states
        row.effects = [e.model_dump() for e in char_state.effects]
        db.add(row)
        db.commit()

    def apply_manual_commands(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        user_input: str,
        commands: list[Command],
    ) -> tuple[bool, str]:
        state = WorldState.model_validate(campaign.state)
        validation = validate_commands(state, commands)
        if validation["rejected"]:
            reason = validation["rejected"][0]["reason"]
            return False, reason

        updated = apply_commands(state, validation["accepted"])
        updated = tick_effects(updated)
        campaign.state = updated.model_dump()

        player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor)
            .one_or_none()
        )
        if player:
            self.sync_inventory_for_player(db, player, updated)
            self.sync_character_row(db, campaign, player, updated)

        turn = TurnLog(
            campaign_id=campaign.id,
            actor=actor,
            user_input=user_input,
            ai_raw_output=json.dumps({"source": "manual_command"}),
            accepted_commands=[c.model_dump() for c in validation["accepted"]],
            rejected_commands=[],
            narration="Manual state change applied.",
        )
        db.add(turn)
        db.add(campaign)
        db.commit()
        return True, "ok"

    def process_turn(
        self,
        db: Session,
        campaign: Campaign,
        actor: str,
        actor_display_name: str,
        user_input: str,
    ) -> tuple[str, dict]:
        player = self.ensure_player(db, campaign, actor, actor_display_name)
        current_state = WorldState.model_validate(campaign.state)
        context_window = self.context_builder.build(db, campaign, actor_id=actor)

        system_prompt = self.build_campaign_system_prompt(db, campaign)
        ai_response = self.llm.generate(
            user_input=user_input,
            state_json=current_state.model_dump_json(),
            mode=campaign.mode,
            context_json=json.dumps(context_window),
            system_prompt=system_prompt,
        )

        validation = validate_commands(current_state, ai_response.commands)
        new_state = apply_commands(current_state, validation["accepted"])
        new_state = tick_effects(new_state)

        campaign.state = new_state.model_dump()
        self.sync_inventory_for_player(db, player, new_state)
        self.sync_character_row(db, campaign, player, new_state)

        turn = TurnLog(
            campaign_id=campaign.id,
            actor=actor,
            user_input=user_input,
            ai_raw_output=ai_response.model_dump_json(),
            accepted_commands=[c.model_dump() for c in validation["accepted"]],
            rejected_commands=validation["rejected"],
            narration=ai_response.narration,
        )
        db.add(turn)
        db.add(campaign)
        db.commit()

        return ai_response.narration, {
            "accepted": [c.model_dump() for c in validation["accepted"]],
            "rejected": validation["rejected"],
            "context": context_window,
        }
