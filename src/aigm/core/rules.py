from __future__ import annotations

DEFAULT_RULES: dict[str, str] = {
    "no_retcon_without_vote": "Story-changing retcons require party agreement.",
    "respect_action_order": "Resolve actions in message order unless GM overrides for fairness.",
    "bounded_power": "Players cannot gain infinite resources, stats, or instant omnipotence.",
    "state_first": "Only validated state commands may mutate game state.",
}

MODE_RULE_PACKS: dict[str, dict[str, str]] = {
    "dnd": {
        "dice_resolution": "Uncertain outcomes should be narrated with risk/cost consistent with tabletop style.",
        "resource_pressure": "Track and respect finite resources such as HP, consumables, and conditions.",
        "tactical_clarity": "Combat and danger scenes should preserve tactical clarity and consequences.",
    },
    "story": {
        "narrative_momentum": "Favor momentum and character-driven progression over granular simulation.",
        "soft_mechanics": "Use light-touch mechanics; avoid over-constraining purely narrative actions.",
        "continuity_focus": "Prioritize continuity of tone, relationships, and scene evolution.",
    },
}

def merge_rules(custom_rules: dict[str, str] | None, mode: str | None = None) -> dict[str, str]:
    merged = dict(DEFAULT_RULES)
    mode_key = (mode or "").strip().lower()
    if mode_key in MODE_RULE_PACKS:
        merged.update(MODE_RULE_PACKS[mode_key])
    if custom_rules:
        merged.update({k: v for k, v in custom_rules.items() if v})
    return merged
