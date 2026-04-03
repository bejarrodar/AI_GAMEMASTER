from __future__ import annotations

import json
from pathlib import Path

from aigm.adapters.llm import LLMAdapter
from aigm.schemas.game import CharacterState, PlayerIntentExtraction, WorldState
from aigm.services.game_service import GameService


def _current_narration_snapshot() -> dict[str, str]:
    svc = GameService(LLMAdapter())
    state = WorldState(scene="Dawn breaks over a frontier town built beside ancient ruins.")
    out: dict[str, str] = {}
    out["story_continuation_failure"] = GameService._build_story_continuation_failure_narration(
        "I pickup a lunchbox and scoop up 10 children into it to save for food later.",
        state,
    )

    intent = PlayerIntentExtraction.model_validate(
        {
            "inventory": [],
            "commands": [],
            "feasibility_checks": [
                {
                    "action": "pickup",
                    "item_key": "ancient ruins",
                    "question": "Can 'ancient ruins' be placed in inventory?",
                    "is_possible": False,
                    "reason": "'ancient ruins' is a structure and is not portable.",
                    "object_type": "structure",
                    "portability": "non_portable",
                }
            ],
        }
    )
    out["infeasible_enforced"] = GameService._enforce_infeasible_intent_on_narration(
        "You heft the ancient ruins and tuck them into your bag.",
        intent,
    )

    out["prompt_leakage_stripped"] = GameService._strip_system_prompt_leakage(
        'You try to lift the nearby house. The Game Master says: "PLAYER AGENCY RULESET (SELECTED BLOCKS) FORMAT: Follow each rule block exactly."'
    )

    state_with_char = WorldState(
        scene="Town square",
        party={
            "Oscar Mayer": CharacterState(
                name="Oscar Mayer",
                description="A cheerful bard in a bright red scarf.",
                hp=10,
                max_hp=10,
                inventory={"lute": 1, "coin_pouch": 1},
            )
        },
    )
    out["appearance_self_inspection"] = svc._self_inspection_narration(
        state_with_char,
        "Oscar Mayer",
        "What do I look like?",
    ) or ""
    out["equipment_self_inspection"] = svc._self_inspection_narration(
        state_with_char,
        "Oscar Mayer",
        "What am I equipped with?",
    ) or ""

    out["empty_inventory_self_inspection"] = svc._self_inspection_narration(
        WorldState(
            scene="Town square",
            party={"Shade": CharacterState(name="Shade", hp=10, max_hp=10, inventory={})},
        ),
        "Shade",
        "show my inventory",
    ) or ""
    return out


def test_narration_quality_snapshots_critical_paths() -> None:
    snapshot_path = Path("tests/snapshots/narration_quality_v1.json")
    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert _current_narration_snapshot() == expected
