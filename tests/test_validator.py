from aigm.core.state_machine import apply_commands, tick_effects
from aigm.core.validator import validate_commands
from aigm.schemas.game import CharacterState, Command, WorldState


def test_reject_unknown_character_adjust_hp() -> None:
    state = WorldState(scene="x", party={"aria": CharacterState(name="aria", hp=10, max_hp=10)})
    cmds = [Command(type="adjust_hp", target="ghost", amount=-2)]

    result = validate_commands(state, cmds)

    assert len(result["accepted"]) == 0
    assert len(result["rejected"]) == 1


def test_reject_remove_missing_item() -> None:
    state = WorldState(
        scene="x",
        party={"aria": CharacterState(name="aria", hp=10, max_hp=10, inventory={"potion": 1})},
    )
    cmds = [Command(type="remove_item", target="aria", key="potion", amount=2)]

    result = validate_commands(state, cmds)

    assert len(result["rejected"]) == 1


def test_apply_valid_sequence() -> None:
    state = WorldState(
        scene="x",
        party={"aria": CharacterState(name="aria", hp=10, max_hp=10, inventory={"potion": 1})},
    )
    cmds = [
        Command(type="adjust_hp", target="aria", amount=-3),
        Command(type="add_item", target="aria", key="potion", amount=2),
    ]

    next_state = apply_commands(state, cmds)

    assert next_state.party["aria"].hp == 7
    assert next_state.party["aria"].inventory["potion"] == 3


def test_effect_lifecycle_with_tick() -> None:
    state = WorldState(scene="x", party={"aria": CharacterState(name="aria", hp=10, max_hp=10)})
    applied = apply_commands(
        state,
        [
            Command(
                type="add_effect",
                target="aria",
                key="broken_arm",
                effect_category="physical",
                duration_turns=2,
                text="Left arm is fractured",
            )
        ],
    )
    assert len(applied.party["aria"].effects) == 1
    assert applied.party["aria"].effects[0].duration_turns == 2

    ticked = tick_effects(applied)
    assert ticked.party["aria"].effects[0].duration_turns == 1

    expired = tick_effects(ticked)
    assert expired.party["aria"].effects == []


def test_item_state_requires_owned_item() -> None:
    state = WorldState(scene="x", party={"aria": CharacterState(name="aria", hp=10, max_hp=10, inventory={})})
    cmds = [
        Command(type="set_item_state", target="aria", key="flame_sword", text="ignited", value=True),
    ]
    result = validate_commands(state, cmds)
    assert len(result["accepted"]) == 0
    assert len(result["rejected"]) == 1
