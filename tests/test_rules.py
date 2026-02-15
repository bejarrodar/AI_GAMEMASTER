from aigm.core.rules import DEFAULT_RULES, MODE_RULE_PACKS, merge_rules


def test_merge_rules_keeps_defaults_when_no_custom() -> None:
    merged = merge_rules(None)
    assert merged == DEFAULT_RULES


def test_merge_rules_overrides_default_and_adds_custom() -> None:
    merged = merge_rules(
        {
            "state_first": "Only the rules engine can modify world state.",
            "table_tone": "Keep content PG-13.",
        }
    )
    assert merged["state_first"] == "Only the rules engine can modify world state."
    assert merged["table_tone"] == "Keep content PG-13."


def test_merge_rules_includes_mode_pack() -> None:
    merged = merge_rules(None, mode="story")
    for key in MODE_RULE_PACKS["story"]:
        assert key in merged
