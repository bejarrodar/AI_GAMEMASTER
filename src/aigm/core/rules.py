from __future__ import annotations

DEFAULT_RULES: dict[str, str] = {
    "no_retcon_without_vote": "Story-changing retcons require party agreement.",
    "respect_action_order": "Resolve actions in message order unless GM overrides for fairness.",
    "bounded_power": "Players cannot gain infinite resources, stats, or instant omnipotence.",
    "state_first": "Only validated state commands may mutate game state.",
}


def merge_rules(custom_rules: dict[str, str] | None) -> dict[str, str]:
    merged = dict(DEFAULT_RULES)
    if custom_rules:
        merged.update({k: v for k, v in custom_rules.items() if v})
    return merged
