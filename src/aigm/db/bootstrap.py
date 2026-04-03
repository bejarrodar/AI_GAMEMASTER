from __future__ import annotations

import argparse

from sqlalchemy import inspect, text

from aigm.config import settings
from aigm.db.init_db import init_db
from aigm.db.session import engine


DEFAULT_REQUIRED_TABLES = (
    "campaigns",
    "system_logs",
    "bot_configs",
    "admin_audit_logs",
    "campaign_memory_summaries",
    "game_rulesets",
    "rulebooks",
    "rulebook_entries",
    "dice_roll_logs",
    "dead_letter_events",
)

REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "campaigns": {
        # Optimistic locking column; required by Campaign.__mapper_args__["version_id_col"].
        "version": "INTEGER NOT NULL DEFAULT 1",
    }
}


def ensure_required_columns() -> bool:
    """Ensure required columns exist on already-created tables.

    This is primarily used to self-heal local SQLite databases that were created
    before newer columns (e.g., `campaigns.version`) were introduced.
    """
    inspector = inspect(engine)
    changed = False
    with engine.begin() as conn:
        for table_name, cols in REQUIRED_COLUMNS.items():
            if not inspector.has_table(table_name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table_name)}
            for col_name, ddl in cols.items():
                if col_name in existing:
                    continue
                conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {ddl}"))
                changed = True
                if table_name == "campaigns" and col_name == "version":
                    conn.execute(text("UPDATE campaigns SET version = 1 WHERE version IS NULL"))
    return changed


def ensure_schema(required_tables: tuple[str, ...] = DEFAULT_REQUIRED_TABLES) -> bool:
    """Ensure DB schema exists for required tables.

    Returns:
        True if schema creation ran, False if schema was already ready.
    """
    if settings.database_use_alembic:
        from aigm.db.migrate import upgrade_head

        upgrade_head()
        return True

    inspector = inspect(engine)
    missing = [name for name in required_tables if not inspector.has_table(name)]
    changed = False
    if missing:
        if not settings.database_auto_init:
            raise RuntimeError(
                "Database schema is missing required tables and AIGM_DATABASE_AUTO_INIT is false. "
                "Run Alembic migrations or enable auto-init for this environment."
            )
        init_db()
        changed = True

    # Even when all required tables exist, columns may be stale in legacy DB files.
    if ensure_required_columns():
        changed = True

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize DB schema only when required tables are missing.")
    parser.add_argument(
        "--required-table",
        action="append",
        dest="required_tables",
        default=[],
        help="Required table name; repeat to specify multiple.",
    )
    args = parser.parse_args()
    required = tuple(args.required_tables) if args.required_tables else DEFAULT_REQUIRED_TABLES
    created = ensure_schema(required_tables=required)
    if created:
        print(f"[db-bootstrap] schema initialized (required tables missing: {', '.join(required)})")
    else:
        print("[db-bootstrap] schema already up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
