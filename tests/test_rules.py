from aigm.core.rules import DEFAULT_RULES, merge_rules


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
