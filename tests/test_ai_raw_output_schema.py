from __future__ import annotations

import json
from types import SimpleNamespace

from aigm.services.game_service import GameService


def test_ai_raw_output_legacy_payload_is_migrated_to_current_schema() -> None:
    raw = GameService.serialize_ai_raw_output({"intent": {"commands": []}})
    payload = json.loads(raw)
    assert int(payload["schema_version"]) == GameService.AI_RAW_OUTPUT_SCHEMA_VERSION
    assert str(payload["source"]) == "migrated_unknown"
    assert int(payload["_migrated_from_schema_version"]) == 1


def test_ai_raw_output_deserialize_invalid_json_returns_error_envelope() -> None:
    payload = GameService.deserialize_ai_raw_output('{"source":"llm_turn",')
    assert int(payload["schema_version"]) == GameService.AI_RAW_OUTPUT_SCHEMA_VERSION
    assert str(payload["source"]) == "serialization_error"
    assert "error" in payload


def test_extract_state_before_uses_deserialize_and_migration() -> None:
    turn = SimpleNamespace(
        ai_raw_output=json.dumps({"source": "llm_turn", "schema_version": 1, "state_before": {"scene": "x"}})
    )
    state_before = GameService._extract_state_before_from_turn(turn)  # noqa: SLF001
    assert state_before == {"scene": "x"}
