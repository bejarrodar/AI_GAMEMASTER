from __future__ import annotations

from copy import deepcopy

from aigm.schemas.game import Command, TimedEffect, WorldState


class StateError(ValueError):
    pass


def _get_actor(state: WorldState, target: str | None):
    if target not in state.party:
        raise StateError(f"Unknown character: {target}")
    return state.party[target]


def apply_commands(state: WorldState, commands: list[Command]) -> WorldState:
    updated = deepcopy(state)

    for cmd in commands:
        if cmd.type == "set_scene":
            if not isinstance(cmd.text, str):
                raise StateError("set_scene requires text")
            updated.scene = cmd.text

        elif cmd.type == "adjust_hp":
            actor = _get_actor(updated, cmd.target)
            if not isinstance(cmd.amount, int):
                raise StateError("adjust_hp requires amount")
            actor.hp = max(0, min(actor.max_hp, actor.hp + cmd.amount))

        elif cmd.type == "add_item":
            actor = _get_actor(updated, cmd.target)
            if not isinstance(cmd.key, str) or not isinstance(cmd.amount, int):
                raise StateError("add_item requires key and amount")
            actor.inventory[cmd.key] = max(0, actor.inventory.get(cmd.key, 0) + cmd.amount)

        elif cmd.type == "remove_item":
            actor = _get_actor(updated, cmd.target)
            if not isinstance(cmd.key, str) or not isinstance(cmd.amount, int):
                raise StateError("remove_item requires key and amount")
            remaining = actor.inventory.get(cmd.key, 0) - cmd.amount
            if remaining < 0:
                raise StateError(f"Cannot remove unavailable item: {cmd.key}")
            if remaining == 0:
                actor.inventory.pop(cmd.key, None)
                actor.item_states.pop(cmd.key, None)
            else:
                actor.inventory[cmd.key] = remaining

        elif cmd.type == "set_item_state":
            actor = _get_actor(updated, cmd.target)
            if not isinstance(cmd.key, str) or cmd.value is None:
                raise StateError("set_item_state requires item key and value")
            if cmd.key not in actor.inventory:
                raise StateError(f"Cannot set state for missing item: {cmd.key}")
            if cmd.text:
                actor.item_states.setdefault(cmd.key, {})[cmd.text] = cmd.value
            else:
                actor.item_states.setdefault(cmd.key, {})["state"] = cmd.value

        elif cmd.type == "set_flag":
            if not isinstance(cmd.key, str):
                raise StateError("set_flag requires key")
            updated.flags[cmd.key] = cmd.value if cmd.value is not None else ""

        elif cmd.type == "set_stat":
            actor = _get_actor(updated, cmd.target)
            if not isinstance(cmd.key, str) or not isinstance(cmd.value, int):
                raise StateError("set_stat requires integer value")
            actor.stats[cmd.key] = cmd.value

        elif cmd.type == "add_effect":
            actor = _get_actor(updated, cmd.target)
            if not isinstance(cmd.key, str):
                raise StateError("add_effect requires effect key")
            if cmd.effect_category not in ("magical", "physical", "misc"):
                raise StateError("add_effect requires effect_category")
            actor.effects = [e for e in actor.effects if e.key != cmd.key]
            actor.effects.append(
                TimedEffect(
                    key=cmd.key,
                    category=cmd.effect_category,
                    description=cmd.text or "",
                    duration_turns=cmd.duration_turns,
                )
            )

        elif cmd.type == "remove_effect":
            actor = _get_actor(updated, cmd.target)
            if not isinstance(cmd.key, str):
                raise StateError("remove_effect requires effect key")
            actor.effects = [e for e in actor.effects if e.key != cmd.key]

        elif cmd.type == "narrate":
            continue

        else:
            raise StateError(f"Unsupported command: {cmd.type}")

    return updated


def tick_effects(state: WorldState) -> WorldState:
    updated = deepcopy(state)
    for name in updated.party:
        next_effects = []
        for effect in updated.party[name].effects:
            if effect.duration_turns is None:
                next_effects.append(effect)
                continue
            turns_left = effect.duration_turns - 1
            if turns_left > 0:
                next_effects.append(
                    TimedEffect(
                        key=effect.key,
                        category=effect.category,
                        description=effect.description,
                        duration_turns=turns_left,
                    )
                )
        updated.party[name].effects = next_effects
    return updated
