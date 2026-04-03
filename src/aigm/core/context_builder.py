from __future__ import annotations

import json
import re

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
            "rules": merge_rules(custom_rules, mode=campaign.mode),
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
            "active_npcs": [
                {
                    "name": n.name,
                    "description": n.description,
                    "disposition": n.disposition,
                    "location": n.location,
                    "flags": n.flags,
                }
                for n in list(state.npcs.values())[: max_characters]
            ],
            "active_locations": [
                {
                    "name": loc.name,
                    "description": loc.description,
                    "tags": loc.tags,
                    "connected_to": loc.connected_to,
                    "flags": loc.flags,
                }
                for loc in list(state.locations.values())[: max_characters]
            ],
            "combat_round": state.combat_round,
        }

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        t = (text or "").strip()
        if len(t) <= limit:
            return t
        return t[: max(0, limit - 3)].rstrip() + "..."

    @staticmethod
    def _tokens(text: str) -> set[str]:
        return {t for t in re.findall(r"[a-z0-9_']+", (text or "").lower()) if len(t) >= 2}

    @classmethod
    def _item_relevance_score(
        cls,
        item_key: str,
        quantity: int,
        user_tokens: set[str],
        scene_tokens: set[str],
        learned_boost: float = 0.0,
    ) -> float:
        score = 0.2
        item_tokens = cls._tokens(item_key.replace("_", " "))
        if item_tokens & user_tokens:
            score += 0.7
        if any(t in scene_tokens for t in {"fight", "combat", "enemy", "danger"}):
            if any(t in item_tokens for t in {"sword", "dagger", "shield", "bow", "arrow"}):
                score += 0.4
        if "torch" in item_tokens:
            dark_hints = {"night", "dark", "dusk", "cave", "forest", "woods", "ruins", "shadow"}
            day_hints = {"day", "dawn", "sun", "noon", "morning"}
            if dark_hints & scene_tokens:
                score += 0.7
            if day_hints & scene_tokens:
                score -= 0.2
        if quantity > 1:
            score += 0.05
        score += max(0.0, learned_boost)
        return max(0.0, score)

    def pack_for_llm(
        self,
        *,
        base_context: dict,
        state: WorldState,
        actor_character_name: str | None,
        user_input: str = "",
        learned_item_relevance: dict[str, float] | None = None,
        learned_effect_relevance: dict[str, float] | None = None,
        long_term_memory: list[dict] | None = None,
        max_facts: int = 12,
        recent_turns: int = 6,
        turn_line_max_chars: int = 180,
        token_budget_chars: int = 0,
        include_truncation_diagnostics: bool = True,
    ) -> dict:
        user_tokens = self._tokens(user_input)
        scene_tokens = self._tokens(state.scene)
        candidates: list[tuple[float, str, str]] = []
        mode = str(base_context.get("mode", "") or "").strip()
        scene = str(state.scene or "").strip()
        if mode:
            candidates.append((0.4, "mode", f"Mode: {mode}"))
        if scene:
            candidates.append((0.8, "scene", f"Scene: {scene}"))
        if state.combat_round:
            candidates.append((0.65, "combat_round", f"Combat round: {state.combat_round}"))
        if actor_character_name:
            candidates.append((1.0, "actor", f"Acting character: {actor_character_name}"))

        actor_state = state.party.get(actor_character_name) if actor_character_name else None
        if actor_state:
            hp_ratio = actor_state.hp / max(1, actor_state.max_hp)
            hp_score = 0.7 if hp_ratio <= 0.4 else 0.45
            candidates.append((hp_score, "actor_hp", f"Actor HP: {actor_state.hp}/{actor_state.max_hp}"))
            if actor_state.inventory:
                for key, qty in actor_state.inventory.items():
                    learned_boost = 0.0
                    if learned_item_relevance:
                        learned_boost = float(learned_item_relevance.get(key, 0.0))
                    s = self._item_relevance_score(key, qty, user_tokens, scene_tokens, learned_boost=learned_boost)
                    candidates.append((s, "actor_inventory_item", f"Actor item: {key} x{qty}"))
            if actor_state.effects:
                for e in actor_state.effects[:8]:
                    score = 0.9 if (self._tokens(e.key) & user_tokens) else 0.6
                    if learned_effect_relevance:
                        score += float(learned_effect_relevance.get(e.key, 0.0))
                    candidates.append((score, "actor_effect", f"Actor effect: {e.key} ({e.category})"))

        other_party = [n for n in state.party.keys() if n != actor_character_name][:6]
        if other_party:
            score = 0.5 if (set(n.lower() for n in other_party) & user_tokens) else 0.25
            candidates.append((score, "other_party", f"Other known characters: {', '.join(other_party)}"))

        if state.npcs:
            for npc_name, npc in list(state.npcs.items())[:6]:
                score = 0.55 if (self._tokens(npc_name) & user_tokens) else 0.28
                if npc.location and (self._tokens(npc.location) & scene_tokens):
                    score += 0.1
                candidates.append((score, "npc", f"NPC: {npc_name} ({npc.disposition}) @ {npc.location or 'unknown'}"))

        if state.locations:
            for loc_name, loc in list(state.locations.items())[:6]:
                score = 0.6 if (self._tokens(loc_name) & user_tokens) else 0.3
                if self._tokens(" ".join(loc.tags)) & user_tokens:
                    score += 0.2
                candidates.append((score, "location", f"Location: {loc_name} [{', '.join(loc.tags[:4])}]"))

        flags = dict(state.flags or {})
        if flags:
            for k in list(flags.keys())[:8]:
                key_tokens = self._tokens(k)
                score = 0.55 if (key_tokens & user_tokens) else 0.3
                candidates.append((score, "flag", f"World flag: {k}={flags[k]}"))

        runtime_constraints = list(base_context.get("runtime_constraints", []) or [])
        for c in runtime_constraints[:4]:
            candidates.append((1.2, "constraint", f"Constraint: {c}"))

        if base_context.get("intent"):
            candidates.append((0.5, "intent_marker", "Intent extracted for current turn."))
        if long_term_memory:
            for row in long_term_memory[-3:]:
                start_id = row.get("start_turn_id")
                end_id = row.get("end_turn_id")
                summary = self._clip(str(row.get("summary", "") or ""), 220)
                if summary:
                    candidates.append((0.52, "long_term_memory", f"Memory {start_id}-{end_id}: {summary}"))

        candidates.sort(key=lambda row: row[0], reverse=True)
        selected = candidates[: max(1, max_facts)]
        facts = [f for _, _, f in selected]

        turns = list(base_context.get("conversation_history", []) or [])
        turns = turns[-max(1, recent_turns) :]
        summary_lines: list[str] = []
        recent_turn_rows: list[dict] = []
        for t in turns:
            actor = str(t.get("actor_name") or t.get("actor_id") or "unknown")
            user_input = self._clip(str(t.get("user_input", "") or ""), turn_line_max_chars // 2)
            narration = self._clip(str(t.get("narration", "") or ""), turn_line_max_chars // 2)
            line = f"{actor}: {user_input}"
            if narration:
                line += f" -> {narration}"
            summary_lines.append(self._clip(line, turn_line_max_chars))
            recent_turn_rows.append({"actor": actor, "user_input": user_input, "narration": narration})

        payload = {
            "campaign_id": base_context.get("campaign_id"),
            "mode": mode,
            "actor_character_name": actor_character_name,
            "relevant_facts": facts,
            "relevance_scored_facts": [
                {"score": round(score, 3), "kind": kind, "fact": fact} for score, kind, fact in selected
            ],
            "turn_summary": "\n".join(summary_lines),
            "recent_turns": recent_turn_rows,
            "runtime_constraints": runtime_constraints,
            "intent": base_context.get("intent", {}),
            "intent_validation": base_context.get("intent_validation", {}),
            "learned_item_relevance": learned_item_relevance or {},
            "learned_effect_relevance": learned_effect_relevance or {},
            "long_term_memory": long_term_memory or [],
        }
        return self._apply_token_budget(
            payload=payload,
            token_budget_chars=max(0, int(token_budget_chars)),
            include_truncation_diagnostics=include_truncation_diagnostics,
        )

    @staticmethod
    def _estimated_chars(payload: dict) -> int:
        try:
            return len(str(payload.get("turn_summary", ""))) + len(json.dumps(payload.get("relevant_facts", [])))
        except Exception:
            return len(str(payload))

    def _apply_token_budget(
        self,
        *,
        payload: dict,
        token_budget_chars: int,
        include_truncation_diagnostics: bool,
    ) -> dict:
        estimated_before = self._estimated_chars(payload)
        diagnostics = {
            "budget_chars": int(token_budget_chars),
            "estimated_chars_before": int(estimated_before),
            "estimated_chars_after": int(estimated_before),
            "truncated": False,
            "dropped_facts": 0,
            "dropped_turns": 0,
            "truncated_turn_summary": False,
        }
        if token_budget_chars <= 0 or estimated_before <= token_budget_chars:
            if include_truncation_diagnostics:
                payload["context_budget"] = diagnostics
            return payload

        facts = list(payload.get("relevant_facts", []) or [])
        scored = list(payload.get("relevance_scored_facts", []) or [])
        turns = list(payload.get("recent_turns", []) or [])
        summary = str(payload.get("turn_summary", "") or "")

        while self._estimated_chars(payload) > token_budget_chars and len(facts) > 1:
            facts.pop()
            if scored:
                scored.pop()
            diagnostics["dropped_facts"] = int(diagnostics["dropped_facts"]) + 1
            diagnostics["truncated"] = True
            payload["relevant_facts"] = facts
            payload["relevance_scored_facts"] = scored

        while self._estimated_chars(payload) > token_budget_chars and len(turns) > 1:
            turns.pop(0)
            diagnostics["dropped_turns"] = int(diagnostics["dropped_turns"]) + 1
            diagnostics["truncated"] = True
            payload["recent_turns"] = turns
            # Rebuild summary to match reduced turns
            rebuilt: list[str] = []
            for row in turns:
                actor = str(row.get("actor", "unknown"))
                user_input = str(row.get("user_input", ""))
                narration = str(row.get("narration", ""))
                line = f"{actor}: {user_input}"
                if narration:
                    line += f" -> {narration}"
                rebuilt.append(line)
            summary = "\n".join(rebuilt)
            payload["turn_summary"] = summary

        if self._estimated_chars(payload) > token_budget_chars and summary:
            keep = max(120, token_budget_chars // 3)
            payload["turn_summary"] = self._clip(summary, keep)
            diagnostics["truncated_turn_summary"] = True
            diagnostics["truncated"] = True

        diagnostics["estimated_chars_after"] = int(self._estimated_chars(payload))
        if include_truncation_diagnostics:
            payload["context_budget"] = diagnostics
        return payload
