from __future__ import annotations

from sqlalchemy.orm import Session

from aigm.core.rules import merge_rules
from aigm.db.models import Campaign, CampaignRule, Character, InventoryItem, Player
from aigm.schemas.game import WorldState


class ContextBuilder:
    """Builds a compact context window for the AI to reduce token load."""

    def build(self, db: Session, campaign: Campaign, actor_id: str, max_characters: int = 4) -> dict:
        state = WorldState.model_validate(campaign.state)

        player = (
            db.query(Player)
            .filter(Player.campaign_id == campaign.id, Player.discord_user_id == actor_id)
            .one_or_none()
        )

        rules_rows = db.query(CampaignRule).filter(CampaignRule.campaign_id == campaign.id).all()
        custom_rules = {r.rule_key: r.rule_value for r in rules_rows}

        names = list(state.party.keys())[:max_characters]
        relevant_party = {name: state.party[name].model_dump() for name in names}

        actor_inventory: list[dict] = []
        if player:
            actor_inventory = [
                {"item_key": i.item_key, "quantity": i.quantity}
                for i in db.query(InventoryItem).filter(InventoryItem.player_id == player.id).all()
            ]

        return {
            "campaign_id": campaign.id,
            "mode": campaign.mode,
            "scene": state.scene,
            "flags": state.flags,
            "rules": merge_rules(custom_rules),
            "actor": {
                "discord_user_id": actor_id,
                "player_name": player.display_name if player else None,
                "inventory": actor_inventory,
            },
            "relevant_party": relevant_party,
            "active_characters": [
                {
                    "name": c.name,
                    "role": c.role,
                    "hp": c.hp,
                    "max_hp": c.max_hp,
                    "effects": c.effects,
                    "item_states": c.item_states,
                }
                for c in db.query(Character).filter(Character.campaign_id == campaign.id).limit(max_characters).all()
            ],
        }
