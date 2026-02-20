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
   - Creates a campaign per thread on first message.
   - Supports runtime thread commands:
     - `!adminauth <token>`
     - `!adminlogout`
     - `!adminrules`
     - `!adminrule add|update|remove ...`
     - `!setrule key=value`
     - `!agencyprofiles`
     - `!setagencyprofile <profile>`
     - `!agencyrules`
     - `!setagencyrules <csv_rule_ids>`
     - `!setcharacter <character instructions>`
     - `!setprompt <custom campaign directives>`
     - `!showprompt`
     - `!rules`
     - `!showruleset`
     - `!setruleset <ruleset_key>`
     - `!rulelookup <query>`
     - `!roll <dice_expression>`
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
   - `admin_audit_logs`: append-only audit trail for admin operations.
   - Global learned relevance + knowledge for items/effects across all campaigns.

## State model details

Each character now includes:
- **Inventory quantities** (`inventory`)
- **Per-item state** (`item_states`) for cases like `ignited=true`
- **Timed effects** (`effects`) categorized as `magical`, `physical`, or `misc`

World-level state now also includes:

- **NPC entities** (`npcs`) with disposition/location/flags
- **Location graph nodes** (`locations`) with tags/links
- **Combat round marker** (`combat_round`) for tactical turn tracking

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
- `!agentmode [classic|crew]` to select per-campaign turn engine
- `!showprompt` to inspect the effective prompt
- `!adminauth <token>` for sys_admin authentication
- `!adminrules` / `!adminrule add|update|remove ...` for system rule CRUD

Story mode note:
- Campaigns started with `!startstory` (or `!startgame story`) default to the `minimal` agency profile unless explicitly overridden by campaign rules.

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

## Authentication / RBAC

The app now includes a database-backed authentication system with:
- users (username/password, optional linked Discord user id)
- roles
- permissions
- role-permission and user-role mappings

Default permissions include:
- `campaign.read`
- `campaign.play`
- `campaign.retry`
- `campaign.write`
- `campaign.import`
- `campaign.export`
- `rules.manage`
- `system.admin`
- `user.manage`

Default roles include:
- `viewer`
- `player`
- `gm`
- `admin`

Enable permission enforcement:
- `AIGM_AUTH_ENFORCE=true`

Optional bootstrap admin on startup:
- `AIGM_AUTH_BOOTSTRAP_ADMIN_USERNAME=<username>`
- `AIGM_AUTH_BOOTSTRAP_ADMIN_PASSWORD=<password>`

When enforcement is enabled:
- Discord command access is checked per permission (with `sys_admin` fallback).
- Discord admin commands can be authorized either by legacy `!adminauth` token flow or by linking a Discord user to an auth user with `system.admin`.
- Streamlit requires login and gates operations by permission.
- Admin tab includes auth user creation, Discord-linking, role assignment, and password reset tools.

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
AIGM_DATABASE_AUTO_INIT=true
AIGM_DATABASE_USE_ALEMBIC=false
AIGM_SYS_ADMIN_TOKEN=change_this_admin_secret
AIGM_AUTH_ENFORCE=false
AIGM_AUTH_BOOTSTRAP_ADMIN_USERNAME=
AIGM_AUTH_BOOTSTRAP_ADMIN_PASSWORD=
AIGM_LLM_PROVIDER=ollama
AIGM_OLLAMA_URL=http://localhost:11434
AIGM_OLLAMA_MODEL=qwen2.5:7b-instruct
AIGM_OLLAMA_MODEL_NARRATION=
AIGM_OLLAMA_MODEL_INTENT=
AIGM_OLLAMA_MODEL_REVIEW=
AIGM_OLLAMA_TIMEOUT_S=180
AIGM_OPENAI_API_KEY=
AIGM_OPENAI_BASE_URL=
AIGM_OPENAI_MODEL=gpt-4o-mini
AIGM_OPENAI_MODEL_NARRATION=
AIGM_OPENAI_MODEL_INTENT=
AIGM_OPENAI_MODEL_REVIEW=
AIGM_OPENAI_TIMEOUT_S=90
AIGM_LLM_JSON_MODE_STRICT=true
AIGM_LLM_HTTP_MAX_RETRIES=2
AIGM_LLM_HTTP_RETRY_BACKOFF_S=0.75
AIGM_DISCORD_RATE_LIMIT_WINDOW_S=10
AIGM_DISCORD_RATE_LIMIT_MAX_MESSAGES=6
AIGM_TURN_CONFLICT_RETRIES=1
AIGM_MANAGEMENT_API_PORT=9541
AIGM_MANAGEMENT_API_IDEMPOTENCY_TTL_S=3600
AIGM_MANAGEMENT_API_IDEMPOTENCY_MAX_ENTRIES=2000
AIGM_DB_API_PORT=9542
AIGM_DB_API_URL=http://127.0.0.1:9542
AIGM_DB_API_TOKEN=
AIGM_COMPONENT_STATE_DIR=./component_state
AIGM_HEALTH_LOG_INTERVAL_S=30
AIGM_HEALTH_ALERT_CONSECUTIVE_FAILURES=3
AIGM_HEALTH_ALERT_WEBHOOK_URL=
AIGM_HEALTH_ALERT_WEBHOOK_COOLDOWN_S=300
AIGM_BACKUP_ENCRYPTION_PASSPHRASE=
AIGM_SECRET_SOURCE=none
AIGM_SECRET_SOURCE_JSON_FILE=
AIGM_SECRET_SOURCE_COMMAND=
AIGM_SECRET_SOURCE_AWS_SECRET_ID=
AIGM_SECRET_SOURCE_AWS_REGION=
AIGM_SECRET_ROTATION_MAX_AGE_DAYS=30
```

LLM provider notes:
- `AIGM_LLM_PROVIDER=ollama` uses the local Ollama backend.
- `AIGM_LLM_PROVIDER=openai` uses OpenAI-compatible chat-completions APIs.
- If `AIGM_OPENAI_BASE_URL` is set, the OpenAI client points to that compatible endpoint.
- `AIGM_LLM_JSON_MODE_STRICT=true` enforces strict JSON response formatting where supported.
- `AIGM_DATABASE_USE_ALEMBIC=true` runs Alembic migrations from startup bootstrap paths.
- `AIGM_DATABASE_AUTO_INIT=false` disables implicit schema creation fallback.
- `*_FILE` secret vars are supported for secure injection in production:
  - `AIGM_DISCORD_TOKEN_FILE`
  - `AIGM_OPENAI_API_KEY_FILE`
  - `AIGM_SYS_ADMIN_TOKEN_FILE`
  - `AIGM_DATABASE_URL_FILE`
- Backup encryption passphrase can also be provided securely:
  - `AIGM_BACKUP_ENCRYPTION_PASSPHRASE`
  - `AIGM_BACKUP_ENCRYPTION_PASSPHRASE_FILE`
- External secret source loaders are supported:
  - `AIGM_SECRET_SOURCE=json_file` + `AIGM_SECRET_SOURCE_JSON_FILE`
  - `AIGM_SECRET_SOURCE=command` + `AIGM_SECRET_SOURCE_COMMAND`
  - `AIGM_SECRET_SOURCE=aws_secrets_manager` + `AIGM_SECRET_SOURCE_AWS_SECRET_ID` (+ optional region)

### 3) Init DB

Option A: Python metadata create-all

```bash
python -m aigm.db.init_db
```

Conditional bootstrap (preferred for startup scripts; only runs when required tables are missing):

```bash
python -m aigm.db.bootstrap --required-table campaigns --required-table system_logs --required-table bot_configs
```

Option B: run SQL migration manually

```bash
psql "$AIGM_DATABASE_URL" -f sql/001_init.sql
```

### 4) Run bot

```bash
python -m aigm.bot
```

## Cloud / OS Installers

The repo now includes dedicated installers for:
- Ubuntu: `scripts/install_ubuntu_stack.sh`
- Debian: `scripts/install_debian_stack.sh`
- Amazon Linux (RHEL-family): `scripts/install_amazon_linux_stack.sh`
- Windows: `scripts/install_windows_stack.ps1`
- Linux component installers:
  - `scripts/install_bot_stack.sh`
  - `scripts/install_web_stack.sh`
  - `scripts/install_llm_stack.sh`
  - `scripts/install_db_stack.sh`
- Windows component installers:
  - `scripts/install_bot_stack.ps1`
  - `scripts/install_web_stack.ps1`
  - `scripts/install_llm_stack.ps1`
  - `scripts/install_db_stack.ps1`

What all installers do by default:
- install dependencies
- install local PostgreSQL (optional)
- install Ollama + pull default model (optional)
- create Python venv and install project deps
- initialize DB schema
- write `.env` defaults
- register/start service(s) for selected components

Linux usage:

```bash
sudo APP_DIR=/opt/ai-gamemaster APP_USER=ubuntu bash ./scripts/install_ubuntu_stack.sh
sudo APP_DIR=/opt/ai-gamemaster APP_USER=debian bash ./scripts/install_debian_stack.sh
sudo APP_DIR=/opt/ai-gamemaster APP_USER=ec2-user bash ./scripts/install_amazon_linux_stack.sh
```

Linux component-mode examples:

```bash
# bot-only host (external DB + external LLM)
sudo COMPONENTS=bot INSTALL_LOCAL_POSTGRES=false INSTALL_LOCAL_OLLAMA=false bash ./scripts/install_bot_stack.sh

# web-only host (external DB + external LLM)
sudo COMPONENTS=web INSTALL_LOCAL_POSTGRES=false INSTALL_LOCAL_OLLAMA=false bash ./scripts/install_web_stack.sh

# llm-only host
sudo bash ./scripts/install_llm_stack.sh
```

Windows usage (run as Administrator PowerShell):

```powershell
.\scripts\install_windows_stack.ps1 -AppDir "D:\AI_GameMaster_code\AI_GAMEMASTER"
```

Windows component-mode examples:

```powershell
# bot-only host
.\scripts\install_bot_stack.ps1 -AppDir "D:\AI_GameMaster_code\AI_GAMEMASTER" -SkipLocalPostgresInstall -SkipLocalOllamaInstall

# web-only host
.\scripts\install_web_stack.ps1 -AppDir "D:\AI_GameMaster_code\AI_GAMEMASTER" -SkipLocalPostgresInstall -SkipLocalOllamaInstall

# llm-only host
.\scripts\install_llm_stack.ps1 -AppDir "D:\AI_GameMaster_code\AI_GAMEMASTER"
```

Common installer overrides:
- `APP_DIR`, `APP_USER`, `VENV_DIR`, `OLLAMA_MODEL`
- `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `STREAMLIT_PORT`
- `COMPONENTS` (`all`, `bot`, `web`, `llm`)
- `INSTALL_LOCAL_POSTGRES` (`true|false`)
- `INSTALL_LOCAL_OLLAMA` (`true|false`)
- `RUN_DB_BOOTSTRAP` (`true|false`)
- `INSTALL_SERVICE` (`true|false`)

Installer validation (Windows host):

```powershell
.\scripts\validate_installers.ps1
```

Optional containerization (not required):
- `deploy/docker-compose.optional.yml` provides profile-based optional container deployment.
- Examples:
  - `docker compose -f deploy/docker-compose.optional.yml --profile all up -d`
  - `docker compose -f deploy/docker-compose.optional.yml --profile bot up -d`
  - `docker compose -f deploy/docker-compose.optional.yml --profile web up -d`
  - `docker compose -f deploy/docker-compose.optional.yml --profile llm up -d`

## Cross-Platform Launcher + Smoke Checks

Use this matrix after install so deployment validation is consistent across environments.

| OS | Start/Restart Services | Service Status | Unified Logs |
|---|---|---|---|
| Ubuntu / Debian / Amazon Linux | `sudo systemctl restart aigm-supervisor` | `sudo systemctl status aigm-supervisor --no-pager` | `sudo journalctl -u aigm-supervisor -n 100 --no-pager` plus files in `AIGM_LOG_DIR` |
| Windows | `sc.exe start aigm-supervisor` | `sc.exe query aigm-supervisor` | Event Viewer plus files in `AIGM_LOG_DIR` |

### Smoke Checks (All Platforms)

1. Health check (supervisor API)
   - Linux/macOS:
     ```bash
     curl -s http://127.0.0.1:9540/health
     ```
   - Windows PowerShell:
     ```powershell
     Invoke-RestMethod -Method Get -Uri http://127.0.0.1:9540/health
     ```

2. Streamlit reachable
   - Linux/macOS:
     ```bash
     curl -I http://127.0.0.1:9531
     ```
   - Windows PowerShell:
     ```powershell
     Invoke-WebRequest -Method Head -Uri http://127.0.0.1:9531
     ```

3. DB ping
   - Linux:
     ```bash
     psql "postgresql://aigm:aigm_password_change_me@localhost:5432/aigm" -c "SELECT 1;"
     ```
   - Windows (if `psql` in PATH):
     ```powershell
     psql "postgresql://aigm:aigm_password_change_me@localhost:5432/aigm" -c "SELECT 1;"
     ```

4. Ollama ping
   - Linux/macOS:
     ```bash
     curl -s http://127.0.0.1:11434/api/tags
     ```
   - Windows PowerShell:
     ```powershell
     Invoke-RestMethod -Method Get -Uri http://127.0.0.1:11434/api/tags
     ```

5. App-level Python ping (DB + settings loaded)
   - All platforms:
     ```bash
     .venv/bin/python -m aigm.db.init_db
     ```
     Windows PowerShell:
     ```powershell
     .\.venv\Scripts\python.exe -m aigm.db.init_db
     ```

### Optional Quick Verification Checklist

- `aigm-supervisor` service is `active/running`.
- Streamlit UI opens and campaign list loads.
- `GET /health` returns checks for DB, Ollama, Streamlit, and child processes.
- `SELECT 1` succeeds against configured DB.
- `GET /api/tags` succeeds against configured Ollama endpoint.
- Discord bot appears online and responds to `!gmhelp` in a thread.

## Local testing environment (PowerShell)

Set up once:

```powershell
.\scripts\setup_local_test_env.ps1
```

Run lint + tests:

```powershell
.\scripts\run_tests.ps1
```

Run the full local stack (DB + Ollama + model + DB init + tests + Streamlit + bot manager):

```powershell
.\scripts\start_local_stack.ps1
```

Notes:
- This script uses a local SQLite database file (default: `.\aigm_local.db`) so Docker/virtualization is not required.
- It checks for `ollama`, installs it with `winget` if missing, then pulls `qwen2.5:7b-instruct` by default.
- For better story quality, you can set `AIGM_OLLAMA_MODEL_NARRATION` to a stronger model (example: `qwen2.5:14b-instruct`) while keeping `AIGM_OLLAMA_MODEL_INTENT` and `AIGM_OLLAMA_MODEL_REVIEW` on a smaller model.
- It writes/updates local `.env` keys for DB + Ollama settings.
- Streamlit port is configurable via `AIGM_STREAMLIT_PORT` (default `9531`).
- If `AIGM_DISCORD_TOKEN` is set, it validates the token and prints `Logged in as ...` before launching Streamlit.
- It starts the unified supervisor (Discord bot manager + Streamlit + health API + unified logs).
- It also starts a versioned management API on `AIGM_MANAGEMENT_API_PORT` (default `9541`).
- Startup scripts run conditional DB bootstrap so schema creation is skipped when required tables already exist.
- Supervisor now includes:
  - log file rotation/retention in `AIGM_LOG_DIR`
  - batched DB writes into `system_logs` to reduce overhead under heavy output
- Streamlit is started in headless mode from this script, so it does not auto-open a browser window.

Start only the Streamlit UI with forced local env (safe for manual reruns):

```powershell
.\scripts\start_streamlit_local.ps1
```

Start only the Discord bot manager with forced local env:

```powershell
.\scripts\start_discord_bot_local.ps1
```

Backup/restore database quickly:

```powershell
.\.venv\Scripts\python.exe .\scripts\backup_db.py
.\.venv\Scripts\python.exe .\scripts\restore_db.py .\backups\your_backup_file.db --force
```

Encrypted backup/restore:

```powershell
.\.venv\Scripts\python.exe .\scripts\backup_db.py --encrypt
.\.venv\Scripts\python.exe .\scripts\restore_db.py .\backups\your_backup_file.db.enc --encrypted --force
```

Run a local backup/restore drill (sqlite):

```powershell
.\.venv\Scripts\python.exe .\scripts\backup_restore_drill.py --passphrase "<your-passphrase>"
```

Rotate local secrets in `.env`:

```powershell
.\.venv\Scripts\python.exe .\scripts\rotate_local_secrets.py --rotate-admin-token --rotate-backup-passphrase
```

## Streamlit management UI

Install UI extras:

```bash
pip install -e .[ui]
```

Run:

```bash
streamlit run streamlit_app.py
```

The UI supports:
- Campaign state inspection.
- Campaign rule override CRUD (same keys as `!setrule`).
- System agency rule block management.
- Per-campaign turn engine selection (`turn_engine`: `classic` or `crew`).
- Crew-style agent definition editing via `agent_crew_definition` JSON rule.
- Crew dry-run/apply actions using the same validation + state machine safety checks.
- LLM Management page:
  - switch provider (`ollama`, `openai`, `stub`)
  - configure task models (narration/intent/review)
  - manage JSON mode strictness
  - list/pull/delete Ollama models
  - list OpenAI-compatible models
  - save all LLM connection/model/runtime settings back to `.env`
- Health page for DB/Ollama/Streamlit/process checks via configured `AIGM_HEALTHCHECK_URL`.
  - Supervisor also exposes Prometheus-style metrics at `/metrics`.
- Management API (auth: `Authorization: Bearer <AIGM_SYS_ADMIN_TOKEN>` when configured):
  - `GET /api/v1/meta`
  - `GET /api/v1/openapi.json`
  - `GET /api/v1/health`
  - `GET|PUT /api/v1/config/llm`
  - `GET|PUT /api/v1/config/web`
  - `GET|POST /api/v1/bots`
  - `PUT|DELETE /api/v1/bots/{id}`
  - `GET /api/v1/logs/system`, `GET /api/v1/logs/audit`
  - `POST /api/v1/debug/checks/db|ollama|openai`
- Dedicated DB API (auth: `Authorization: Bearer <AIGM_DB_API_TOKEN>` when configured):
  - `GET /db/v1/health`
  - `GET|POST /db/v1/bots`
  - `PUT|DELETE /db/v1/bots/{id}`
  - `GET /db/v1/logs/system`
  - `POST /db/v1/logs/system/batch`
  - `GET /db/v1/logs/audit`
  - `GET /db/v1/debug/table-counts`
- System Logs page with filters (`service`, `level`, time window, message search) backed by `system_logs`.
- Bot Manager page for adding/editing/enabling multiple Discord bots using one shared DB (`bot_configs`).
- Users & Roles page for auth user CRUD, role assignment, Discord linking, role creation, and permission mapping.
- Documentation page that renders section files from `docs/*.md`.
- Gameplay & Knowledge page:
  - assign campaign ruleset
  - upsert/list rulesets
  - upsert/list rulebooks and entries
  - run rulebook lookup queries
  - test dice expressions and inspect dice roll logs
- Admin connection management:
  - test/save DB URL
  - persist DB settings to `.env` from UI
- restart notice for applying changes

## Validation Reference

Use this section as the source of truth for quick validation checks after changes/deployments.

- Discord command handlers implemented in `src/aigm/bot.py`:
  - `!adminauth`, `!adminlogout`
  - `!adminrules`, `!adminrule add|update|remove`
  - `!setrule`, `!rules`
  - `!agencyprofiles`, `!setagencyprofile`, `!agencyrules`, `!setagencyrules`
  - `!setcharacter`, `!setprompt`, `!showprompt`
  - `!showruleset`, `!setruleset`, `!rulelookup`, `!roll`
  - `!mycharacter`, `!additem`, `!addeffect`
- Streamlit pages implemented in `streamlit_app.py`:
  - `Campaign Console`
  - `Health`
  - `LLM Management`
  - `Gameplay & Knowledge`
  - `Item Tracker`
  - `System Logs`
  - `Bot Manager`
  - `Users & Roles`
  - `Admin`
  - `Documentation`
- Schema/bootstrap maintenance:
  - `python -m aigm.db.bootstrap` validates required tables and applies init/migration path.
  - Startup scripts run bootstrap + backend seed validation on launch.

### Multi-Bot Runtime

- Bot tokens can be configured in Streamlit under **Bot Manager** (table: `bot_configs`).
- `aigm.ops.bot_manager` now fetches enabled bot configs through DB API (`AIGM_DB_API_URL`), not direct DB sessions.
- If no enabled bot config exists, manager falls back to `AIGM_DISCORD_TOKEN` for backward compatibility.
- All bots share the same campaign database.

### Logging Tunables

- `AIGM_LOG_DIR`: filesystem log output directory
- `AIGM_LOG_FILE_MAX_BYTES`: rotate active log once this size is reached
- `AIGM_LOG_FILE_BACKUP_COUNT`: number of rotated backups retained per log
- `AIGM_LOG_DB_BATCH_SIZE`: batch size for DB writes into `system_logs`
- `AIGM_LOG_DB_FLUSH_INTERVAL_S`: max flush interval for pending DB log rows

## Documentation

The Streamlit **Documentation** page is organized into these sections:

- `LLM Setup`
- `Streamlit Management UI`
- `Bot Management`
- `Discord Commands`
- `Gameplay and Knowledge`
- `Auth and Roles`
- `Health and Logs`
- `World Model`
- `Long Horizon Memory`
- `Model Eval`
- `HA DR Runbook`
- `Deployment Hardening`
- `Installers and Startup`
- `Full README`

Each Documentation section is backed by its own markdown file:

- `docs/LLM_SETUP.md`
- `docs/STREAMLIT_MANAGEMENT_UI.md`
- `docs/BOT_MANAGEMENT.md`
- `docs/DISCORD_COMMANDS.md`
- `docs/GAMEPLAY_KNOWLEDGE.md`
- `docs/AUTH_AND_ROLES.md`
- `docs/HEALTH_AND_LOGS.md`
- `docs/API_CONTRACTS_AND_VERSIONING.md`
- `docs/LOG_SCHEMA.md`
- `docs/SLO_SLI_ALERTING.md`
- `docs/WORLD_MODEL.md`
- `docs/LONG_HORIZON_MEMORY.md`
- `docs/MODEL_EVAL.md`
- `docs/HA_DR_RUNBOOK.md`
- `docs/DEPLOYMENT_HARDENING.md`
- `docs/INSTALLERS_AND_STARTUP.md`
- `docs/FULL_README.md`

## Current and Future Enhancements / Requirements

Use this as the active backlog of enhancements and requirements. Remove items from this list as they are implemented.

- [ ] Release engineering: add blue/green deployment and rollback automation for bot/supervisor releases.
- [x] Reliability: define formal SLOs/SLIs (turn latency, success rate, health uptime) with alert routing and on-call escalation docs.
- [ ] Reliability: add periodic disaster-recovery drill automation (restore rehearsal + verification reports).
- [x] Reliability: add circuit breakers around LLM provider failures and queue overload with graceful degradation modes.
- [ ] Reliability: add idempotent message processing keys to prevent duplicate turn application on retries/reconnects.
- [ ] Reliability: add dead-letter queue + replay tooling for failed Discord events/turn jobs.
- [x] Reliability: implement async worker queue for turn processing to isolate Discord event loop from heavy inference paths.
- [x] Discord command surface: restore explicit pre-start game lifecycle commands (`!startgame`, `!startstory`) with thread gating.
- [x] Discord command surface: restore `!gmhelp` command discovery/help output.
- [x] Discord command surface: restore `!ping` health command.
- [x] Discord command surface: restore campaign snapshot commands (`!exportcampaign`, `!importcampaign`, `!adminrestorecampaign`).
- [x] Discord command surface: restore turn retry command (`!retry`) with state rollback guarantees.
- [x] Discord command surface: restore `!teach` command and LLM-based unknown-command validation/suggestions.
- [x] Discord command surface: restore per-campaign engine command (`!agentmode [classic|crew]`).
- [x] Reliability: add automated chaos tests for DB outage, Ollama outage, and slow LLM responses.
- [x] Observability: add distributed correlation IDs across Discord message -> turn -> LLM calls -> DB writes.
- [x] Observability: add structured JSON logging baseline and log schemas for all services/scripts.
- [x] Observability: move component log writes to API calls (`management_api`/`db_api`) so all persisted logs flow through versioned endpoints instead of direct DB sessions.
- [x] Observability: add logging coverage audit + checklist to identify unlogged paths and enforce minimum log events for startup, command handling, API mutations, retries/fallbacks, and failures.
- [x] Observability: add multiline error/traceback ingestion contract across all components (preserve single logical error entries end-to-end in DB/UI/API).
- [x] Observability: add alert rules for stalled turns, repeated fallback usage, and token/latency anomalies.
- [ ] Security: add MFA/SSO options for Streamlit admin accounts and session timeout/lockout policies.
- [ ] Security: add audit review tools (search/export/signing) for `admin_audit_logs`.
- [x] Security: add rate limits and abuse controls for admin endpoints and dangerous commands.
- [ ] Security: add encryption-at-rest guidance and key-rotation runbook for backups/secrets.
- [ ] Data governance: add PII redaction controls for logs, prompts, and exports.
- [ ] Data governance: add retention policies for turn logs, audit logs, system logs, and memory summaries.
- [x] API/platform: add versioned admin API (health, bots, config, logs, debug checks) for non-UI management.
- [x] API/platform: add campaign/gameplay DB API endpoints and migrate `GameService` reads/writes behind a service client.
- [x] API/platform: migrate Streamlit pages to use only Management/DB APIs (remove direct `SessionLocal` usage in UI layer).
- [ ] API/platform: split supervisor into independently deployable `web`, `bot`, `management_api`, `db_api` service units with explicit inter-service URLs/tokens.
- [ ] API/platform: expand management API to full users/roles CRUD + permission scope mapping endpoints.
- [x] API/platform: add OpenAPI docs for management endpoints and auth scopes.
- [x] API/platform: publish API schemas/version contracts (request/response) and add backward-compatibility policy.
- [ ] API/platform: add inter-service auth scope separation (distinct tokens/scopes per component, token rotation workflow).
- [ ] API/platform: add API rate limiting and per-endpoint quotas for management/debug surfaces.
- [x] API/platform: add API idempotency keys for mutating endpoints (`POST`/`PUT`/`DELETE`) where retries are expected.
- [x] API/platform: add service-to-service retry/circuit-breaker policy with consistent error envelope across APIs.
- [x] API/platform: add event/audit correlation IDs propagated across API hops (bot -> management -> db_api).
- [x] API/platform: add API integration test suite covering cross-component flows (bot config CRUD -> bot manager reconcile -> health/log visibility).
- [ ] Multi-tenant readiness: define tenant boundaries and row-level isolation strategy for shared deployments.
- [ ] Multi-bot platform: add per-bot config profiles (models, prompts, rate limits, allowed commands, visibility).
- [ ] Multi-bot platform: add bot lifecycle management (drain mode, maintenance mode, migration between threads).
- [x] State safety: complete command-level transactional guarantees so all mutations are atomic per turn.
- [ ] State safety: add full rewind/replay tooling (per-turn rollback, selective replay, conflict handling).
- [x] State safety: add schema validation for all persisted `ai_raw_output` payloads with migration/versioning.
- [x] LLM robustness: add response contract tests for intent extraction/review/generation JSON modes across providers.
- [ ] LLM robustness: add adaptive fallback routing across local and external LLM providers by latency/quality targets.
- [ ] LLM robustness: add prompt template versioning with A/B testing and rollback.
- [x] LLM robustness: replace scene-affordance pickup heuristics with explicit LLM feasibility checks (item availability + rationale + confidence), keeping deterministic fallback only for outage paths.
- [x] LLM robustness: migrate inventory intent parsing from regex-first to LLM-first (regex only as emergency extractor fallback with no silent state mutation).
- [x] LLM robustness: replace deterministic object portability classifier with model-provided `object_type`/`portability` + confidence calibration.
- [ ] LLM robustness: migrate self-query trigger detection (`appearance`/`equipped`) to intent classification while preserving deterministic answers from saved state/inventory.
- [x] LLM robustness: replace heuristic purchase/currency checks with ruleset-aware transaction intent extraction (cost, currency, quantity) and explicit validation.
- [ ] LLM robustness: shift relevance-learning fallback extraction to intent-native `relevance_signals` as primary source.
- [ ] LLM robustness: keep string-similarity command inference as last-resort fallback only; enforce confidence thresholds on LLM command suggestion.
- [ ] LLM roadmap: design and train a dedicated intent-to-JSON model specialized for command/turn parsing reliability.
- [ ] LLM roadmap: design a dual-model architecture (`parser model` + `story model`) with separate eval suites and routing policy.
- [ ] Performance: add token budgeting/enforcement with hard caps and context truncation diagnostics.
- [ ] Performance: add response streaming path (Discord typing/partial status updates) for long generations.
- [ ] Performance: add benchmark suite for end-to-end throughput by mode (`dnd`, `story`, `crew`).
- [ ] Gameplay systems: expand dice engine beyond current baseline (contested rolls, saved roll presets, richer roll expression grammar, GM-forced rolls).
- [ ] Gameplay systems: expand configurable ruleset packs beyond current baseline (additional systems/editions, deeper mechanics metadata, compatibility layers).
- [ ] Gameplay systems: add initiative/order/combat toolkit and encounter state tracking.
- [ ] Gameplay systems: add economy subsystem (currency validation, pricing tables, purchase checks).
- [ ] Knowledge systems: expand rulebook ingestion pipeline with stronger provenance/citation enforcement and bulk import tooling.
- [ ] Knowledge systems: expand retrieval/ranking with confidence scoring and citation quality controls.
- [ ] Knowledge systems: add approval workflow + provenance history for custom lore/books authoring.
- [ ] Knowledge systems: add world encyclopedia entities (items, factions, locations, spells, effects) with admin curation.
- [ ] GM profiles: add selectable GM styles (genre + personality presets) and per-campaign style overrides.
- [ ] GM profiles: add safety/style guardrails per profile (tone bounds, violence bounds, forbidden content).
- [ ] UX/theming: add theme manager (icons, palettes, typography, layout presets) with user-level preferences.
- [ ] UX/theming: add campaign presentation skins (fantasy/sci-fi/horror) and visual assets mapping.
- [ ] UX/theming: add accessibility pass (contrast, keyboard nav, reduced motion, screen-reader labels).
- [ ] Collaboration: add concurrent speaker handling policy (message ordering windows, simultaneous action resolution).
- [ ] Collaboration: add GM moderation tools (pause/resume thread, soft-delete turn, annotate rulings).
- [ ] Import/export: add granular export/import scopes (characters, rules, inventory, world, logs, memories, bots).
- [ ] Import/export: add signed/verified backup bundles for secure transfer between threads/servers.
- [ ] Testing/quality: add nightly soak tests with long-running simulated campaigns and memory growth checks.
- [ ] Testing/quality: add contract tests for Discord command parsing, help suggestions, and unknown-command intent mapping.
- [ ] Testing/quality: add snapshot regression tests for narration quality across critical scenarios.
- [ ] Cost control: add token/cost dashboards and per-campaign quotas with admin override workflows.
- [ ] Operations: add one-command diagnostics bundle script for support incidents (config, health, logs, metrics, recent errors).
- [ ] Operations: add issue tracking page in Streamlit with triage workflow (status, severity, owner, resolution notes) backed by `!reportissue` and admin-created issues.

### Priority Execution Order

Use this sequence for implementation and validation so the platform remains testable while expanding scope.

#### Immediate Attention (stability + correctness first)

All previously listed immediate items are complete. Continue with Basic Testing Readiness items below.

#### Basic Testing Readiness (production-like validation baseline)

1. Reliability: define formal SLOs/SLIs (turn latency, success rate, health uptime) with alert routing and on-call escalation docs.
2. Observability: add structured JSON logging baseline and log schemas for all services/scripts.
3. Observability: move component log writes to API calls (`management_api`/`db_api`) so all persisted logs flow through versioned endpoints instead of direct DB sessions.
4. Observability: add alert rules for stalled turns, repeated fallback usage, and token/latency anomalies.
5. API/platform: publish API schemas/version contracts (request/response) and add backward-compatibility policy.
6. API/platform: add OpenAPI docs for management endpoints and auth scopes.
7. API/platform: add API idempotency keys for mutating endpoints (`POST`/`PUT`/`DELETE`) where retries are expected.
8. Testing/quality: add contract tests for Discord command parsing, help suggestions, and unknown-command intent mapping.
9. Testing/quality: add snapshot regression tests for narration quality across critical scenarios.
10. Testing/quality: add nightly soak tests with long-running simulated campaigns and memory growth checks.
11. Operations: add one-command diagnostics bundle script for support incidents (config, health, logs, metrics, recent errors).

#### Nice-to-Have (scale, UX, and expansion)

1. API/platform: split supervisor into independently deployable `web`, `bot`, `management_api`, `db_api` service units with explicit inter-service URLs/tokens.
2. API/platform: expand management API to full users/roles CRUD + permission scope mapping endpoints.
3. API/platform: add inter-service auth scope separation (distinct tokens/scopes per component, token rotation workflow).
4. API/platform: add API rate limiting and per-endpoint quotas for management/debug surfaces.
5. Multi-bot platform: add per-bot config profiles (models, prompts, rate limits, allowed commands, visibility).
6. Multi-bot platform: add bot lifecycle management (drain mode, maintenance mode, migration between threads).
7. State safety: add full rewind/replay tooling (per-turn rollback, selective replay, conflict handling).
8. LLM robustness: add adaptive fallback routing across local and external LLM providers by latency/quality targets.
9. LLM robustness: add prompt template versioning with A/B testing and rollback.
10. LLM robustness: migrate self-query trigger detection (`appearance`/`equipped`) to intent classification while preserving deterministic answers from saved state/inventory.
11. LLM robustness: shift relevance-learning fallback extraction to intent-native `relevance_signals` as primary source.
12. LLM robustness: keep string-similarity command inference as last-resort fallback only; enforce confidence thresholds on LLM command suggestion.
13. LLM roadmap: design and train a dedicated intent-to-JSON model specialized for command/turn parsing reliability.
14. LLM roadmap: design a dual-model architecture (`parser model` + `story model`) with separate eval suites and routing policy.
15. Performance: add token budgeting/enforcement with hard caps and context truncation diagnostics.
16. Performance: add response streaming path (Discord typing/partial status updates) for long generations.
17. Performance: add benchmark suite for end-to-end throughput by mode (`dnd`, `story`, `crew`).
18. Gameplay systems: expand dice engine beyond current baseline (contested rolls, saved roll presets, richer roll expression grammar, GM-forced rolls).
19. Gameplay systems: expand configurable ruleset packs beyond current baseline (additional systems/editions, deeper mechanics metadata, compatibility layers).
20. Gameplay systems: add initiative/order/combat toolkit and encounter state tracking.
21. Gameplay systems: add economy subsystem (currency validation, pricing tables, purchase checks).
22. Knowledge systems: expand rulebook ingestion pipeline with stronger provenance/citation enforcement and bulk import tooling.
23. Knowledge systems: expand retrieval/ranking with confidence scoring and citation quality controls.
24. Knowledge systems: add approval workflow + provenance history for custom lore/books authoring.
25. Knowledge systems: add world encyclopedia entities (items, factions, locations, spells, effects) with admin curation.
26. GM profiles: add selectable GM styles (genre + personality presets) and per-campaign style overrides.
27. GM profiles: add safety/style guardrails per profile (tone bounds, violence bounds, forbidden content).
28. UX/theming: add theme manager (icons, palettes, typography, layout presets) with user-level preferences.
29. UX/theming: add campaign presentation skins (fantasy/sci-fi/horror) and visual assets mapping.
30. UX/theming: add accessibility pass (contrast, keyboard nav, reduced motion, screen-reader labels).
31. Collaboration: add concurrent speaker handling policy (message ordering windows, simultaneous action resolution).
32. Collaboration: add GM moderation tools (pause/resume thread, soft-delete turn, annotate rulings).
33. Import/export: add granular export/import scopes (characters, rules, inventory, world, logs, memories, bots).
34. Import/export: add signed/verified backup bundles for secure transfer between threads/servers.
35. Cost control: add token/cost dashboards and per-campaign quotas with admin override workflows.
36. Operations: add issue tracking page in Streamlit with triage workflow (status, severity, owner, resolution notes) backed by `!reportissue` and admin-created issues.
37. Security: add MFA/SSO options for Streamlit admin accounts and session timeout/lockout policies.
38. Security: add audit review tools (search/export/signing) for `admin_audit_logs`.
39. Security: add encryption-at-rest guidance and key-rotation runbook for backups/secrets.
40. Data governance: add PII redaction controls for logs, prompts, and exports.
41. Data governance: add retention policies for turn logs, audit logs, system logs, and memory summaries.
42. Reliability: add periodic disaster-recovery drill automation (restore rehearsal + verification reports).
43. Release engineering: add blue/green deployment and rollback automation for bot/supervisor releases.
44. Multi-tenant readiness: define tenant boundaries and row-level isolation strategy for shared deployments.

Already implemented:

- Discord turn processing runs heavy turn work in worker threads via `asyncio.to_thread`.
- LLM HTTP calls now have configurable retry/backoff (`AIGM_LLM_HTTP_MAX_RETRIES`, `AIGM_LLM_HTTP_RETRY_BACKOFF_S`).
- Discord per-user/thread rate limiting is enforced (`AIGM_DISCORD_RATE_LIMIT_WINDOW_S`, `AIGM_DISCORD_RATE_LIMIT_MAX_MESSAGES`).
- CI includes dependency vulnerability auditing via `pip-audit`.
- Campaign optimistic locking is enabled with `campaigns.version`.
- Alembic migration scaffolding is included (`alembic.ini`, `alembic/`, `src/aigm/db/migrate.py`) with optional startup usage via `AIGM_DATABASE_USE_ALEMBIC=true`.
- Health API now exposes Prometheus-style metrics on `/metrics`.
- Management API is implemented and versioned (`/api/v1/*`) for config, bot management, logs, and debug checks.
- Management API now publishes OpenAPI docs at `/api/v1/openapi.json` with auth scope mapping.
- API contract/versioning and backward-compatibility policy are documented in `docs/API_CONTRACTS_AND_VERSIONING.md`.
- Management API mutation idempotency keys are supported for retry-safe `POST`/`PUT`/`DELETE` flows.
- DB API client now applies retry/backoff + circuit-breaker policy for service-to-service calls (`AIGM_SERVICE_API_HTTP_*`, `AIGM_SERVICE_API_CIRCUIT_BREAKER_*`) and uses a consistent API error envelope (`error_code`, `error_message`, `error_details`) across Management/DB APIs.
- Correlation IDs are now propagated across Management API -> DB API hops (`X-Correlation-ID`) and echoed in responses/errors for end-to-end traceability.
- Dedicated DB API is implemented and versioned (`/db/v1/*`) for DB-backed bot config/log access and DB diagnostics.
- Bot manager now uses DB API for bot config reads (API call boundary instead of direct DB session).
- Management API now uses DB API for bot config CRUD/log queries and DB health checks.
- Gameplay DB API endpoints now include campaign-by-thread, campaign-rule read/write, and idempotency reservation surfaces (`/db/v1/campaigns/*`, `/db/v1/idempotency/reserve`), with `GameService` client-backed usage when enabled (`AIGM_GAMEPLAY_USE_DB_API=true`).
- Duplicate message/turn application guard is enforced via `ProcessedDiscordMessage` idempotency reservation in Discord turn flow.
- DB API now includes campaign-by-id, global item/effect knowledge/relevance, and dice-roll read surfaces (`/db/v1/campaigns/by-id`, `/db/v1/knowledge/*`, `/db/v1/dice-rolls`).
- Turn routing now applies explicit rollback envelopes on failure (`source=turn_rollback`) and writes rollback diagnostics to `turn_logs` while returning a safe retry message.
- Command-level transactional envelopes are in place for turn processing, including rollback diagnostics in persisted turn logs.
- Management API now includes auth user/role/permission endpoints and agency-rule + crew-turn endpoints used by Streamlit admin surfaces.
- API integration tests cover cross-component DB API <-> Management API flows (bot CRUD, log visibility, health checks).
- `ai_raw_output` payloads now pass versioned schema normalization/validation on serialize/deserialize with legacy migration to the current schema version.
- LLM contract tests now validate intent extraction, generation, and review JSON envelopes across both Ollama and OpenAI adapter paths.
- Intent extraction is now LLM-first with regex intent enrichment limited to emergency fallback mode only.
- Runtime portability gating now uses model-provided feasibility metadata (and explicit feasibility calls when missing) rather than deterministic classifier overrides.
- Purchase/currency validation now relies on intent transaction metadata (`requires_payment`, `cost_amount`, `currency`, `has_required_funds`) instead of regex/currency heuristics.
- Pickup feasibility no longer uses scene-affordance heuristics; uncertain pickup intents now trigger explicit feasibility assessment (LLM-first, deterministic fallback only on model/provider outage).
- Component-local configuration store exists for standalone component wiring (`AIGM_COMPONENT_STATE_DIR`).
- Supervisor now runs DB API as a separate subprocess component and monitors it in health/process checks.
- DB-only installer entrypoints are available for separated local DB deployments (`scripts/install_db_stack.sh`, `scripts/install_db_stack.ps1`).
- Supervisor health alerts are configurable (`AIGM_HEALTH_LOG_INTERVAL_S`, `AIGM_HEALTH_ALERT_CONSECUTIVE_FAILURES`).
- Supervisor can send health alerts to webhook endpoints (`AIGM_HEALTH_ALERT_WEBHOOK_URL`, `AIGM_HEALTH_ALERT_WEBHOOK_COOLDOWN_S`).
- Supervisor runtime alerts are available for stalled turns, fallback spikes, and latency anomalies (`AIGM_ALERT_*` settings).
- Supervisor traceback coalescing now handles prefixed subprocess lines (e.g., bot-manager stderr prefixes) so multi-line exceptions persist as a single logical log entry.
- Turn success/failure counters and latency sums are exported on `/metrics` (plus log queue depth gauge).
- Streamlit UI now uses Management/DB APIs for campaign/gameplay/auth/admin operations without direct `SessionLocal` DB access in the UI layer.
- Management API now includes rate limits for read and mutation paths (`AIGM_MANAGEMENT_API_RATE_LIMIT_*`).
- Discord turn handling now uses a bounded async worker queue (`AIGM_TURN_WORKER_QUEUE_MAX`, `AIGM_TURN_WORKER_COUNT`) with graceful queue-full responses.
- LLM provider circuit-breaker safeguards are implemented (`AIGM_LLM_CIRCUIT_BREAKER_FAILURE_THRESHOLD`, `AIGM_LLM_CIRCUIT_BREAKER_RESET_S`).
- Logging coverage checklist documentation is available at `docs/LOGGING_COVERAGE_CHECKLIST.md`.
- Structured logging schema baseline is documented at `docs/LOG_SCHEMA.md` (`aigm.log.v1`).
- Baseline SLO/SLI and alert routing targets are documented at `docs/SLO_SLI_ALERTING.md`.
- Supervisor system-log persistence now uses DB API ingestion (`POST /db/v1/logs/system/batch`) instead of direct DB sessions.
- Turn conflict retries are configurable (`AIGM_TURN_CONFLICT_RETRIES`).
- Secret file loading is supported for key credentials (`*_FILE` variables).
- External secret sources are supported (`json_file`, `command`, `aws_secrets_manager`).
- Secret governance checks include rotation-age health validation (`AIGM_SECRET_ROTATION_MAX_AGE_DAYS`).
- Secret source access and local secret rotation events are auditable in `admin_audit_logs`.
- Admin actions are audited to `admin_audit_logs` (Discord and Streamlit management flows).
- A lightweight load/perf harness is available at `scripts/load_test_turns.py`.
- Long-horizon narrative memory/archival summarization is implemented via `campaign_memory_summaries`.
- DB backup/restore helper scripts are available (`scripts/backup_db.py`, `scripts/restore_db.py`).
- Encrypted backup/restore is supported via passphrase (`scripts/backup_db.py --encrypt`, `scripts/restore_db.py --encrypted`).
- Local backup/restore drill script is available (`scripts/backup_restore_drill.py`).
- High-availability and DR guidance is documented (`docs/HA_DR_RUNBOOK.md`).
- Local secret rotation helper is available (`scripts/rotate_local_secrets.py`).
- World model includes NPCs, locations, and combat round support (`WorldState` extensions).
- Per-mode rule packs are merged automatically (`dnd` and `story`) with campaign overrides.
- Baseline gameplay systems are implemented:
  - dice engine with modifiers and advantage/disadvantage (`GameService.roll_dice`) plus persisted roll logs (`dice_roll_logs`)
  - campaign ruleset management with seeded defaults (`dnd5e-2014`, `dnd5e-2024`, `story-freeform`)
- Baseline knowledge systems are implemented:
  - rulebook and entry storage (`rulebooks`, `rulebook_entries`)
  - rulebook lookup/retrieval APIs used by prompts and commands (`search_rulebook_entries`, `rule_lookup_for_campaign`)
  - seed pipeline for default gameplay/rulebook knowledge (`seed_default_gameplay_knowledge`)
- Gameplay and Knowledge surfaces are implemented:
  - Streamlit page `Gameplay & Knowledge` for rulesets/rulebooks/dice logs
  - Discord commands `!showruleset`, `!setruleset`, `!rulelookup`, `!roll`
- Formal model-evaluation regression harness is available (`scripts/model_eval_regression.py`, `scripts/model_eval_cases.json`).
- CI gates run lint, tests, health endpoint tests, `pip-audit`, and `pip check`.
- Chaos resilience tests now cover DB API outage/retry/circuit-breaker behavior plus Ollama outage/timeout fallback paths (`tests/test_chaos_resilience.py`).

## Design notes for your goals

- **Strict game state**: state transitions happen only via validated commands.
- **Flexible gameplay**: narration is unconstrained, but mutations are constrained.
- **Anti-hallucination**: impossible commands are rejected + logged.
- **Token efficiency**: context builder sends only relevant state to the AI.
- **Rule flexibility**: defaults are always present; campaign-specific rules can override them.
- **Group-first model**: no baked-in single hero; each player can declare their own character naturally.
