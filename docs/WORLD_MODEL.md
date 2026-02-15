# World Model

The runtime world state now supports:

- `party`: player-linked characters (existing)
- `npcs`: non-player entities with disposition/location/flags
- `locations`: named places with tags and links
- `combat_round`: optional round counter for tactical scenes

## Schema Notes

See `src/aigm/schemas/game.py`:

- `NPCState`
- `LocationState`
- `WorldState`

## Rule Packs by Mode

Rule merging now includes mode packs:

- `dnd` pack: tactical clarity, resource pressure, uncertain outcomes
- `story` pack: narrative momentum, soft mechanics, continuity focus

Custom campaign rules still override defaults and mode-pack rules where keys match.

## Context Packing

`ContextBuilder.pack_for_llm` includes NPC/location/combat-round facts in relevance scoring, so prompts can include richer scene context with bounded token size.
