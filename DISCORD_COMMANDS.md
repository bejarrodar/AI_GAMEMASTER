# Discord Command Reference

This file documents all Discord commands implemented by the bot in `src/aigm/bot.py`.

## Scope

- Commands are processed only in **thread channels** (`public_thread` or `private_thread`).
- In non-thread channels, `!` commands receive a guidance message.
- A thread must be started with `!startgame` before the bot responds to normal prompts or other commands.
- Before game start, only `!startgame`, `!startstory`, `!gmhelp`, `!adminauth`, `!importcampaign`, and `!adminrestorecampaign` are processed.
- If `AIGM_AUTH_ENFORCE=true`, command access is permission-gated by RBAC (with sys_admin fallback).

## Health / Utility

- `!gmhelp`
  - Lists available GameMaster commands with short descriptions and usage.

- `!startgame [dnd|story]`
  - Starts a game in the current thread.
  - Optional mode argument defaults to `dnd`.

- `!startstory`
  - Starts a game in `story` mode with a lighter default rule profile for interactive storytelling.

- `!importcampaign <snapshot_key>`
  - Imports a previously exported campaign snapshot into the current thread.
  - Works before game start so a deleted/recreated thread can be restored.

- `!adminrestorecampaign <snapshot_key>`
  - Admin-only restore/import into the current thread.
  - Designed for private/admin-only threads and disaster recovery.

- `!ping`
  - Replies with runtime status details (`mode`, `started`, `engine`, `thread`).
  - Use this to confirm bot responsiveness and current campaign state in a thread.

## Campaign Rules

- `!setrule key=value`
  - Sets or updates a campaign rule override for the current thread campaign.

- `!rules`
  - Shows the effective merged rules for the campaign.

- `!showruleset`
  - Shows the active gameplay ruleset for the campaign (for example `dnd5e-2014`).

- `!setruleset <ruleset_key>`
  - Sets the active gameplay ruleset for the campaign.
  - Requires `campaign.write` permission.

- `!rulelookup <query>`
  - Searches rulebook knowledge for the active campaign ruleset and returns top matches.
  - Useful for quickly looking up checks/combat/condition handling in thread.

- `!roll <dice_expression>`
  - Rolls dice and reports the result.
  - Supported examples: `d20`, `2d6+3`, `4d8-1`, `adv d20+5`, `dis d20`.

- `!retry`
  - Reverts your most recent turn by restoring full `state_before` snapshot.
  - Re-runs that same input to generate a fresh response.
  - Intended for "try again" with strict state rollback (inventory, effects, stats, scene, flags, NPC/world data in state).

- `!exportcampaign [all|state|rules|players|characters|turns]`
  - Exports selected campaign scope and returns a snapshot key.
  - Use that key with `!importcampaign` or `!adminrestorecampaign` in another thread.

## Agency Rule Profiles and Selection

- `!agencyprofiles`
  - Lists available agency rule profiles (`minimal`, `balanced`, `full` by default).

- `!setagencyprofile <profile>`
  - Sets campaign profile-based agency rules.
  - Clears custom `agency_rule_ids`.

- `!agencyrules`
  - Lists currently available **enabled** agency rule IDs.

- `!setagencyrules <csv_rule_ids>`
  - Sets explicit agency rule IDs for this campaign (overrides profile).
  - Rejects unknown/disabled rule IDs.

## Prompt Customization

- `!setcharacter <character instructions>`
  - Sets campaign `character_instructions` used by system prompt building.

- `!setprompt <custom campaign directives>`
  - Sets campaign `system_prompt_custom` directives.

- `!showprompt`
  - Shows the effective runtime system prompt (truncated for Discord message length safety).

## Turn Engine Mode

- `!agentmode`
  - Shows current per-campaign turn engine (`classic` or `crew`).

- `!agentmode <classic|crew>`
  - Sets current campaign turn engine.
  - `classic`: single-pass `process_turn`
  - `crew`: multi-step `process_turn_with_crew` using `agent_crew_definition` rule

## Character Registration

- `!mycharacter <natural language character description>`
  - Generates and registers/updates the calling player's linked character in campaign state.
  - Ensures unique character name in party.

- `!deletecharacter`
  - Deletes your linked player character from campaign state.
  - Clears your normalized inventory rows for this campaign.

## Manual State Commands

- `!additem <character_name> <item_key> <quantity>`
  - Applies validated manual `add_item` command for the target character.

- `!addeffect <character>|<magical|physical|misc>|<effect_key>|<duration_or_none>|<description>`
  - Applies validated manual `add_effect` command.
  - `duration_or_none` can be `none` or an integer.

- `!teach <item|effect>|<key>|<observation>`
  - Records a global observed instance into the knowledge tracker.
  - Allowed kinds: `item`, `effect`.
  - Updates knowledge and relevance signals indirectly from observation text/context.
  - Does **not** accept or apply direct numeric weight/relevance overrides.

## Sys Admin Authentication / Rule Management

- `!adminauth <token>`
  - Authenticates current Discord user as sys admin for this runtime session.
  - Requires token match with `AIGM_SYS_ADMIN_TOKEN`.
  - Optional when RBAC is enabled and your linked auth user has `system.admin`.

- `!adminlogout`
  - Clears current runtime admin session for the user.

- `!adminrules`
  - Lists all system agency rule blocks.
  - Admin-only.

- `!adminrule add <rule_id>|<title>|<priority>|<body>`
  - Creates or upserts a system agency rule block.
  - Admin-only.

- `!adminrule update <rule_id>|<title>|<priority>|<body>`
  - Updates an existing system agency rule block (upsert behavior in service).
  - Admin-only.

- `!adminrule remove <rule_id>`
  - Removes a system agency rule block.
  - Admin-only.

## Notes

- All narrative turns run through validation/state application safeguards.
- Invalid mutation commands are rejected before state is changed.
- Some commands may return usage guidance on malformed input.
- Unknown `!` commands are passed to LLM command inference to suggest the closest valid command when possible.
