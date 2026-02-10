from __future__ import annotations

import json
import re

from aigm.schemas.game import AIResponse, CharacterState, WorldState


class LLMAdapter:
    """Replace this stub with OpenAI/Anthropic/etc provider calls."""

    def generate(self, user_input: str, state_json: str, mode: str, context_json: str, system_prompt: str) -> AIResponse:
        _ = (state_json, mode, context_json, system_prompt)
        fallback = {
            "narration": f"You said: {user_input}. The world reacts.",
            "commands": [{"type": "narrate", "text": user_input}],
        }
        return AIResponse.model_validate(json.loads(json.dumps(fallback)))

    def generate_world_seed(self, mode: str) -> WorldState:
        if mode == "story":
            scene = "Rain taps across neon signs as rumors spread through a crowded skyport market."
        else:
            scene = "Dawn breaks over a frontier town built beside ancient ruins."
        return WorldState(scene=scene, flags={"mode": mode}, party={})

    def generate_character_from_description(self, description: str, fallback_name: str) -> CharacterState:
        hp = 12 if "tank" in description.lower() else 10
        stats = {"str": 10, "dex": 10, "int": 10}
        if "wizard" in description.lower() or "mage" in description.lower():
            stats["int"] = 14
        if "rogue" in description.lower() or "thief" in description.lower():
            stats["dex"] = 14
        if "fighter" in description.lower() or "knight" in description.lower():
            stats["str"] = 14

        possible = re.findall(r"name\s+is\s+([A-Za-z][A-Za-z\-']+)", description, flags=re.IGNORECASE)
        name = possible[0] if possible else fallback_name
        return CharacterState(name=name, hp=hp, max_hp=hp, stats=stats, inventory={}, item_states={}, effects=[])
