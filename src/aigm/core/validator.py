from __future__ import annotations

from aigm.core.state_machine import StateError, apply_commands
from aigm.schemas.game import Command, WorldState


class ValidationResult(dict):
    accepted: list[Command]
    rejected: list[dict[str, str]]


def validate_commands(state: WorldState, proposed: list[Command]) -> ValidationResult:
    accepted: list[Command] = []
    rejected: list[dict[str, str]] = []

    for cmd in proposed:
        try:
            apply_commands(state, accepted + [cmd])
            accepted.append(cmd)
        except StateError as exc:
            rejected.append({"command": cmd.model_dump_json(), "reason": str(exc)})

    result = ValidationResult()
    result["accepted"] = accepted
    result["rejected"] = rejected
    return result
