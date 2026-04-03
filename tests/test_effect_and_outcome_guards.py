from aigm.adapters.llm import LLMAdapter
from aigm.core.state_machine import tick_effects
from aigm.schemas.game import CharacterState, Command, TimedEffect, WorldState
from aigm.services.game_service import GameService


def test_tick_effects_applies_poison_dot() -> None:
    state = WorldState(
        scene="test",
        flags={},
        party={
            "Shade": CharacterState(
                name="Shade",
                hp=10,
                max_hp=10,
                effects=[TimedEffect(key="poisoned", category="physical", description="poison", duration_turns=2)],
            )
        },
    )
    updated = tick_effects(state)
    assert updated.party["Shade"].hp == 9
    assert len(updated.party["Shade"].effects) == 1
    assert updated.party["Shade"].effects[0].duration_turns == 1


def test_outcome_guard_rejects_harmful_commands_on_miss_narration() -> None:
    service = GameService(LLMAdapter())
    commands = [
        Command(type="adjust_hp", target="Bear", amount=-3),
        Command(type="add_effect", target="Bear", key="poisoned", effect_category="physical", duration_turns=3),
    ]
    kept, rejected = service._filter_commands_for_narrative_outcome(
        commands,
        "The dagger misses and hits the wall beside Bear.",
    )
    assert kept == []
    assert len(rejected) == 2


def test_outcome_guard_keeps_non_harmful_effect_on_miss_narration() -> None:
    service = GameService(LLMAdapter())
    commands = [
        Command(type="add_effect", target="Shade", key="magelight", effect_category="magical", duration_turns=5),
    ]
    kept, rejected = service._filter_commands_for_narrative_outcome(
        commands,
        "The strike misses and glances off the wall.",
    )
    assert len(kept) == 1
    assert kept[0].key == "magelight"
    assert rejected == []
