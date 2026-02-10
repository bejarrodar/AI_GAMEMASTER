# AI_GAMEMASTER

AI Game Master backend for Discord thread-based games, with **strict state validation** and persistent PostgreSQL state.

## What this gives you

- Discord bot loop that listens in thread channels.
- PostgreSQL persistence for campaigns, turns, players, inventories, character effects, and feedback.
- A Python sublayer that validates model output before state mutation.
- A deterministic state machine to prevent hallucinated state changes.
- Flexible mode concept (`dnd`, `story`, etc.) while preserving strict state consistency.
- Campaign rule system with default rules + per-thread custom overrides.
- Built-in player-agency system prompt template with campaign-level customization.
- AI-generated campaign seed scene and AI-assisted player character generation from natural language.

## Architecture

1. **Discord adapter** (`src/aigm/bot.py`)
   - Receives player messages in a thread.
   - Loads/creates campaign state by thread ID.
   - Supports runtime thread commands:
     - `!setrule key=value`
     - `!rules`
     - `!mycharacter <natural language description>`
     - `!additem <character_name> <item_key> <quantity>`
     - `!addeffect <character>|<magical|physical|misc>|<effect_key>|<duration_or_none>|<description>`
   - Runs a turn through `GameService`.

2. **Game service orchestration** (`src/aigm/services/game_service.py`)
   - Upserts player profile for Discord user in campaign.
   - Builds a compact relevant context window (scene, actor, selected party, rules, actor inventory).
   - Uses AI for campaign world seed and character generation from player NL description.
   - Requests candidate narration + commands from LLM adapter.
   - Validates commands against current state.
   - Applies only accepted commands.
   - Tracks effect durations turn-by-turn.
   - Syncs validated inventory + character state into normalized tables.
   - Logs raw and filtered outputs for observability.

3. **State safety layer**
   - `src/aigm/core/state_machine.py`: deterministic application of commands.
   - `src/aigm/core/validator.py`: rejects invalid commands.
   - `src/aigm/core/rules.py`: default rulebook and custom-rule merge logic.
   - `src/aigm/core/context_builder.py`: only sends relevant context to LLM.

4. **Persistence layer** (`src/aigm/db/models.py`)
   - `campaigns`: current game state per thread.
   - `turn_logs`: full turn audit trail.
   - `players`: Discord users in a campaign.
   - `characters`: canonical character rows, including item states and active effects.
   - `inventory_items`: normalized inventory per player.
   - `campaign_rules`: custom rule overrides by thread.
   - `feedback`: quality feedback for model tuning.

## State model details

Each character now includes:
- **Inventory quantities** (`inventory`)
- **Per-item state** (`item_states`) for cases like `ignited=true`
- **Timed effects** (`effects`) categorized as `magical`, `physical`, or `misc`

Example: a sword can track `item_states["flame_sword"]["ignited"] = true`, and a character can carry a physical effect like `broken_arm` with a duration.


## Customizable system prompt

The runtime now builds a structured system prompt from:
- selected **Player Agency Protection Rule blocks** (not all rules at once)
- campaign-level `character_instructions`
- campaign-level `system_prompt_custom` directives

Agency rule block selection options:
- profile mode via `agency_rule_profile` (`minimal`, `balanced`, `full`)
- explicit CSV rule IDs via `agency_rule_ids` (overrides profile)

Set these in Discord:
- `!setcharacter ...`
- `!setprompt ...`
- `!agencyprofiles` / `!setagencyprofile ...`
- `!agencyrules` / `!setagencyrules ...`
- `!showprompt` to inspect the effective prompt
- `!adminauth <token>` for sys_admin authentication
- `!adminrules` / `!adminrule add|update|remove ...` for system rule CRUD

Internally this is implemented in `src/aigm/core/prompts.py` and wired via `GameService.build_campaign_system_prompt`.

## Data contract from AI layer

The LLM should output JSON matching `AIResponse` (`src/aigm/schemas/game.py`):

```json
{
  "narration": "The goblin ducks behind a broken cart.",
  "commands": [
    {"type": "adjust_hp", "target": "aria", "amount": -2},
    {"type": "set_item_state", "target": "aria", "key": "flame_sword", "text": "ignited", "value": true},
    {"type": "add_effect", "target": "aria", "key": "broken_arm", "effect_category": "physical", "duration_turns": 3, "text": "Her shield arm is fractured."}
  ]
}
```

Anything invalid is rejected before touching game state.

## Cloudflared-proxied Postgres notes

The DB layer includes TLS settings for proxied/secured Postgres links:

- `AIGM_DATABASE_URL`
- `AIGM_DATABASE_SSLMODE` (default: `require`)
- `AIGM_DATABASE_CONNECT_TIMEOUT_S` (default: `10`)

This allows secure connections when Postgres sits behind a Cloudflare/Cloudflared path.

## Quickstart

### 1) Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

### 2) Configure

Create `.env`:

```env
AIGM_DISCORD_TOKEN=your_discord_bot_token
AIGM_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/aigm
AIGM_DATABASE_SSLMODE=require
AIGM_DATABASE_CONNECT_TIMEOUT_S=10
AIGM_SYS_ADMIN_TOKEN=change_this_admin_secret
```

### 3) Init DB

Option A: Python metadata create-all

```bash
python -m aigm.db.init_db
```

Option B: run SQL migration manually

```bash
psql "$AIGM_DATABASE_URL" -f sql/001_init.sql
```

### 4) Run bot

```bash
python -m aigm.bot
```

## Extending this to production

- Replace `LLMAdapter` stub (`src/aigm/adapters/llm.py`) with your provider and JSON schema/tool-calling.
- Add optimistic locking/version columns for concurrent thread writes.
- Move from `create_all` to Alembic migrations.
- Add richer world model (NPC entities, locations, combat rounds).
- Add per-mode rule packs (e.g., strict DnD, loose story mode).

## Design notes for your goals

- **Strict game state**: state transitions happen only via validated commands.
- **Flexible gameplay**: narration is unconstrained, but mutations are constrained.
- **Anti-hallucination**: impossible commands are rejected + logged.
- **Token efficiency**: context builder sends only relevant state to the AI.
- **Rule flexibility**: defaults are always present; campaign-specific rules can override them.
- **Group-first model**: no baked-in single hero; each player can declare their own character naturally.
