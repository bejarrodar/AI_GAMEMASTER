from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from aigm.db import bootstrap
from aigm.db.base import Base
from aigm.db import models  # noqa: F401


def test_ensure_schema_skips_when_required_tables_exist(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(bootstrap, "engine", engine)
    called = {"value": False}

    def _fake_init_db() -> None:
        called["value"] = True

    monkeypatch.setattr(bootstrap, "init_db", _fake_init_db)
    created = bootstrap.ensure_schema(required_tables=("campaigns", "system_logs", "bot_configs"))
    assert created is False
    assert called["value"] is False


def test_ensure_schema_runs_when_required_table_missing(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    monkeypatch.setattr(bootstrap, "engine", engine)
    called = {"value": False}

    def _fake_init_db() -> None:
        called["value"] = True

    monkeypatch.setattr(bootstrap, "init_db", _fake_init_db)
    created = bootstrap.ensure_schema(required_tables=("campaigns",))
    assert created is True
    assert called["value"] is True


def test_ensure_schema_raises_when_missing_and_auto_init_disabled(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    monkeypatch.setattr(bootstrap, "engine", engine)
    monkeypatch.setattr(bootstrap.settings, "database_auto_init", False)
    monkeypatch.setattr(bootstrap.settings, "database_use_alembic", False)
    try:
        with pytest.raises(RuntimeError, match="AIGM_DATABASE_AUTO_INIT is false"):
            bootstrap.ensure_schema(required_tables=("campaigns",))
    finally:
        monkeypatch.setattr(bootstrap.settings, "database_auto_init", True)
