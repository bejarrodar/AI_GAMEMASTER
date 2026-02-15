from __future__ import annotations

import argparse

from sqlalchemy import inspect

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
)


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
    if not missing:
        return False
    if not settings.database_auto_init:
        raise RuntimeError(
            "Database schema is missing required tables and AIGM_DATABASE_AUTO_INIT is false. "
            "Run Alembic migrations or enable auto-init for this environment."
        )
    init_db()
    return True


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
