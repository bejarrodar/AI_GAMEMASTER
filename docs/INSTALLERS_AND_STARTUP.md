# Installers and Startup

Installers are available for:

- Windows
- Ubuntu
- Debian
- Amazon Linux

## Installer Scripts

- `scripts/install_windows_stack.ps1`
- `scripts/install_ubuntu_stack.sh`
- `scripts/install_debian_stack.sh`
- `scripts/install_amazon_linux_stack.sh`
- component installers:
  - Linux: `scripts/install_bot_stack.sh`, `scripts/install_web_stack.sh`, `scripts/install_llm_stack.sh`, `scripts/install_db_stack.sh`
  - Windows: `scripts/install_bot_stack.ps1`, `scripts/install_web_stack.ps1`, `scripts/install_llm_stack.ps1`, `scripts/install_db_stack.ps1`

## Installer Modes

- `COMPONENTS=all|bot|web|llm`
- optional local dependencies:
  - `INSTALL_LOCAL_POSTGRES=true|false`
  - `INSTALL_LOCAL_OLLAMA=true|false`
- bootstrap and service control:
  - `RUN_DB_BOOTSTRAP=true|false`
  - `INSTALL_SERVICE=true|false`

This allows:
- full single-host deployments
- bot-only hosts using external DB/LLM
- web-only hosts using external DB/LLM
- llm-only hosts

## Local Startup (PowerShell)

- full stack: `scripts/start_local_stack.ps1`
- Streamlit only: `scripts/start_streamlit_local.ps1`
- Discord bot manager only: `scripts/start_discord_bot_local.ps1`

## Startup Expectations

- DB bootstrap/migration validation runs when enabled
- backend default seed validation runs when Python app components are installed
- configured LLM endpoint reachable (local or external)
- health endpoint returns service checks
- management API endpoint returns component/config/debug surfaces (`/api/v1/meta`)
- DB API endpoint returns database-backed resources (`/db/v1/health`)
- logs written to filesystem and DB

## Optional Containerization

Containers are optional and not required.

- compose file: `deploy/docker-compose.optional.yml`
- profile examples:
  - `docker compose -f deploy/docker-compose.optional.yml --profile all up -d`
  - `docker compose -f deploy/docker-compose.optional.yml --profile bot up -d`
  - `docker compose -f deploy/docker-compose.optional.yml --profile web up -d`
  - `docker compose -f deploy/docker-compose.optional.yml --profile llm up -d`

Notes:

- `deploy/docker-compose.optional.yml` now requires `AIGM_POSTGRES_PASSWORD` to be supplied from the environment instead of shipping a hardcoded default.
- In production-style deployments, also set `AIGM_SYS_ADMIN_TOKEN` and `AIGM_DB_API_TOKEN`. The services are designed to fail closed when those tokens are required but absent.

## Backup and Restore

Run from project root:

- backup: `.\.venv\Scripts\python.exe .\scripts\backup_db.py`
- restore: `.\.venv\Scripts\python.exe .\scripts\restore_db.py <backup_file> --force`
- encrypted backup: `.\.venv\Scripts\python.exe .\scripts\backup_db.py --encrypt`
- encrypted restore: `.\.venv\Scripts\python.exe .\scripts\restore_db.py <backup_file>.enc --encrypted --force`
- drill (sqlite): `.\.venv\Scripts\python.exe .\scripts\backup_restore_drill.py --passphrase "<passphrase>"`

Notes:

- For SQLite, restore overwrites the DB file when `--force` is provided.
- For PostgreSQL, restore uses `pg_restore --clean --if-exists`; ensure services are stopped during restore drills.

## Secret Rotation (Local)

- rotate admin token + backup passphrase:
  - `.\.venv\Scripts\python.exe .\scripts\rotate_local_secrets.py --rotate-admin-token --rotate-backup-passphrase`
- write rotation audit to DB:
  - `.\.venv\Scripts\python.exe .\scripts\rotate_local_secrets.py --rotate-admin-token --audit-db --actor-id ops --actor-display "Ops User"`
