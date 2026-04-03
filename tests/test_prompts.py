from aigm.core.prompts import RULE_PROFILES, build_system_prompt, rule_ids_for_profile, selected_rules_text


def test_prompt_contains_selected_rules_and_sections() -> None:
    prompt = build_system_prompt(
        "You are grim.",
        "Keep mystery high.",
        rule_ids=["R1_absolute_agency_boundary", "R9_pause_over_agency_violation"],
    )
    assert "PLAYER AGENCY RULESET (SELECTED BLOCKS)" in prompt
    assert "TITLE: Absolute Agency Boundary" in prompt
    assert "TITLE: Pause Over Violation" in prompt
    assert "You are grim." in prompt
    assert "Keep mystery high." in prompt


def test_prompt_fallbacks() -> None:
    prompt = build_system_prompt("", "")
    assert "adaptive, fair, high-agency Game Master" in prompt
    assert "(none)" in prompt


def test_rule_profile_mapping() -> None:
    assert rule_ids_for_profile("minimal") == RULE_PROFILES["minimal"]
    assert rule_ids_for_profile("unknown") == RULE_PROFILES["balanced"]


def test_selected_rules_text_fallback_for_empty_list() -> None:
    text = selected_rules_text([])
    assert "TITLE: Absolute Agency Boundary" in text
    assert "TITLE: Pause Over Violation" in text


def test_balanced_profile_includes_no_other_pc_puppeteering() -> None:
    prompt = build_system_prompt("You are fair.", "Keep tension high.")
    assert "TITLE: No Puppeteering Other Player Characters" in prompt
