from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TimedEffect(BaseModel):
    key: str
    category: Literal["magical", "physical", "misc"]
    description: str = ""
    duration_turns: int | None = Field(default=None, ge=1)


class CharacterState(BaseModel):
    name: str
    hp: int = Field(ge=0, le=999)
    max_hp: int = Field(ge=1, le=999)
    stats: dict[str, int] = Field(default_factory=dict)
    inventory: dict[str, int] = Field(default_factory=dict)
    item_states: dict[str, dict[str, str | int | bool]] = Field(default_factory=dict)
    effects: list[TimedEffect] = Field(default_factory=list)


class WorldState(BaseModel):
    scene: str = ""
    flags: dict[str, str | int | bool] = Field(default_factory=dict)
    party: dict[str, CharacterState] = Field(default_factory=dict)


class Command(BaseModel):
    type: Literal[
        "narrate",
        "set_scene",
        "adjust_hp",
        "add_item",
        "remove_item",
        "set_item_state",
        "set_flag",
        "set_stat",
        "add_effect",
        "remove_effect",
    ]
    target: str | None = None
    key: str | None = None
    value: str | int | bool | None = None
    amount: int | None = None
    text: str | None = None
    effect_category: Literal["magical", "physical", "misc"] | None = None
    duration_turns: int | None = Field(default=None, ge=1)


class AIResponse(BaseModel):
    narration: str
    commands: list[Command] = Field(default_factory=list)
