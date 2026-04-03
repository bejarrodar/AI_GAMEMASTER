# Gameplay and Knowledge

This subsystem adds structured gameplay mechanics and searchable rule knowledge.

## Included in this batch

- game ruleset registry (`game_rulesets`)
- rulebook storage (`rulebooks`, `rulebook_entries`)
- dice roll logging (`dice_roll_logs`)
- campaign-level ruleset assignment
- rulebook lookup for prompt/context + Discord command

## Discord commands

- `!showruleset`
- `!setruleset <ruleset_key>`
- `!rulelookup <query>`
- `!roll <dice_expression>`

## Streamlit page

Use **Gameplay & Knowledge** to:

- assign a campaign ruleset
- upsert rulesets
- upsert rulebooks and entries
- test rule lookups
- test dice expressions and inspect recent roll logs

## Seeded defaults

- Rulesets:
  - `dnd5e-2014`
  - `dnd5e-2024`
  - `story-freeform`
- Rulebook:
  - `dnd5e-srd-basics` with starter entries for checks/advantage/death saves
