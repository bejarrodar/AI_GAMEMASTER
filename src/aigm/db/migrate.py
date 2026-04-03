from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_head(config_path: str = "alembic.ini") -> None:
    cfg_path = Path(config_path)
    if not cfg_path.exists():
        raise RuntimeError(f"Alembic config not found: {cfg_path}")
    cfg = Config(str(cfg_path))
    command.upgrade(cfg, "head")

