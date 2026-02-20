from __future__ import annotations

import json
from types import SimpleNamespace

from aigm.adapters.llm import LLMAdapter
from aigm.config import settings
from aigm.schemas.game import CharacterState
from aigm.schemas.game import PlayerIntentExtraction
from aigm.schemas.game import WorldState
from aigm.services.game_service import GameService


def test_generate_contract_ollama_coerces_command_shape(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "ollama")

    def _fake_urlopen(_req, timeout_s):  # noqa: ANN001
        _ = timeout_s
        return {
            "message": {
                "content": json.dumps(
                    {
                        "narration": "You act.",
                        "commands": [{"type": "command", "text": "raw command should coerce to narrate"}],
                    }
                )
            }
        }

    monkeypatch.setattr(LLMAdapter, "_urlopen_json_with_retry", staticmethod(_fake_urlopen))
    adapter = LLMAdapter()
    out = adapter.generate(
        user_input="I act",
        state_json="{}",
        mode="dnd",
        context_json="{}",
        system_prompt="test",
    )
    assert out.narration
    assert len(out.commands) == 1
    assert out.commands[0].type == "narrate"


def test_generate_contract_openai_coerces_command_shape(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")

    def _fake_chat_json(*, task, system_prompt, user_prompt):  # noqa: ANN001
        _ = (task, system_prompt, user_prompt)
        return {"narration": "You act.", "commands": [{"type": "action", "text": "normalize"}]}

    monkeypatch.setattr(LLMAdapter, "_chat_json_with_openai", staticmethod(_fake_chat_json))
    adapter = LLMAdapter()
    out = adapter.generate(
        user_input="I act",
        state_json="{}",
        mode="dnd",
        context_json="{}",
        system_prompt="test",
    )
    assert out.narration
    assert len(out.commands) == 1
    assert out.commands[0].type == "narrate"


def test_extract_intent_contract_openai_maps_transaction_fields(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")

    def _fake_chat_json(*, task, system_prompt, user_prompt):  # noqa: ANN001
        _ = (task, system_prompt, user_prompt)
        return {
            "inventory": [{"action": "add", "item_key": "healing_potion", "quantity": 1, "owner": "self"}],
            "commands": [],
            "feasibility_checks": [
                {
                    "action": "add",
                    "item_key": "healing_potion",
                    "question": "Can purchase complete?",
                    "is_possible": False,
                    "reason": "Not enough gold",
                    "object_type": "consumable",
                    "portability": "portable",
                    "requires_payment": True,
                    "cost_amount": 50,
                    "currency": "gold",
                    "payer_owner": "self",
                    "has_required_funds": False,
                }
            ],
        }

    monkeypatch.setattr(LLMAdapter, "_chat_json_with_openai", staticmethod(_fake_chat_json))
    adapter = LLMAdapter()
    intent = adapter.extract_player_intent("I buy a potion", "{}", "{}", "test")
    assert isinstance(intent, PlayerIntentExtraction)
    assert intent.inventory[0].action == "add"
    check = intent.feasibility_checks[0]
    assert check.requires_payment is True
    assert check.cost_amount == 50
    assert check.currency == "gold"
    assert check.has_required_funds is False


def test_extract_intent_llm_first_disables_regex_mutation_when_model_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")

    def _fake_extract(_self, user_input, state_json, context_json, system_prompt):  # noqa: ANN001
        _ = (user_input, state_json, context_json, system_prompt)
        return PlayerIntentExtraction.model_validate({"inventory": [], "commands": [], "feasibility_checks": []})

    monkeypatch.setattr(LLMAdapter, "_extract_player_intent_with_openai", _fake_extract)
    adapter = LLMAdapter()
    intent = adapter.extract_player_intent("I pull the dagger out of my inventory", "{}", "{}", "test")
    assert intent.inventory == []


def test_extract_intent_regex_enrichment_only_in_emergency_fallback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")

    def _boom(_self, user_input, state_json, context_json, system_prompt):  # noqa: ANN001
        _ = (user_input, state_json, context_json, system_prompt)
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(LLMAdapter, "_extract_player_intent_with_openai", _boom)
    adapter = LLMAdapter()
    intent = adapter.extract_player_intent("I pull the dagger out of my inventory", "{}", "{}", "test")
    assert any(r.action == "use" and r.item_key == "dagger" for r in intent.inventory)


def test_review_contract_openai_returns_outputreview(monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_provider", "openai")

    def _fake_chat_json(*, task, system_prompt, user_prompt):  # noqa: ANN001
        _ = (task, system_prompt, user_prompt)
        return {
            "plausible": True,
            "breaks_pc_autonomy": False,
            "violations": [],
            "revised_narration": "Revised response",
            "input_aligned": True,
            "alignment_score": 0.9,
        }

    monkeypatch.setattr(LLMAdapter, "_chat_json_with_openai", staticmethod(_fake_chat_json))
    adapter = LLMAdapter()
    review = adapter.review_output("input", "narration", "{}", "{}", "system")
    assert review.revised_narration == "Revised response"
    assert review.input_aligned is True


def test_fallback_pickup_no_scene_affordance_heuristic() -> None:
    intent = LLMAdapter._fallback_extract_player_intent("I pick up a dagger", '{"scene":"dense forest"}')
    pickup_checks = [c for c in intent.feasibility_checks if c.action == "pickup" and c.item_key == "dagger"]
    assert pickup_checks
    check = pickup_checks[0]
    assert check.is_possible is False
    assert "explicit feasibility assessment" in check.reason.lower()
    assert check.object_type == "unknown"
    assert check.portability == "unknown"


def test_pickup_unresolved_check_triggers_explicit_assessment(monkeypatch) -> None:
    service = GameService(LLMAdapter())
    monkeypatch.setattr(service, "player_character_name", lambda _db, campaign_id, player_id: "Hero")
    monkeypatch.setattr(
        service.llm,
        "assess_inventory_action_feasibility",
        lambda **kwargs: {
            "is_possible": True,
            "reason": "Scene supports this item.",
            "object_type": "small_object",
            "portability": "portable",
            "confidence": 0.9,
        },
    )
    state = WorldState(
        scene="Town square",
        party={"Hero": CharacterState(name="Hero", hp=10, max_hp=10, inventory={}, item_states={}, effects=[])},
    )
    intent = PlayerIntentExtraction.model_validate(
        {
            "inventory": [{"action": "pickup", "item_key": "dagger", "quantity": 1, "owner": "scene"}],
            "commands": [],
            "feasibility_checks": [
                {
                    "action": "pickup",
                    "item_key": "dagger",
                    "question": "Can the player find dagger?",
                    "is_possible": False,
                    "reason": "Requires explicit feasibility assessment from model/context.",
                    "object_type": "unknown",
                    "portability": "unknown",
                }
            ],
        }
    )
    result = service._resolve_inventory_actions_from_intent(  # noqa: SLF001
        db=None,
        campaign=SimpleNamespace(id=1),
        player=SimpleNamespace(id=1),
        current_state=state,
        intent=intent,
        user_input="I pick up a dagger",
    )
    assert result is not None
    assert result["accepted"]
    assert result["accepted"][0].type == "add_item"
    assert result["accepted"][0].key == "dagger"
