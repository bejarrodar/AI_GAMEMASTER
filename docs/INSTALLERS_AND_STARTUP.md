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

## Local Startup (PowerShell)

- full stack: `scripts/start_local_stack.ps1`
- Streamlit only: `scripts/start_streamlit_local.ps1`
- Discord bot manager only: `scripts/start_discord_bot_local.ps1`

## Startup Expectations

- local DB initialized (or bootstrap skipped if already initialized)
- configured LLM endpoint reachable
- health endpoint returns service checks
- logs written to filesystem and DB

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
