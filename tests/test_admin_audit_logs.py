from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aigm.adapters.llm import LLMAdapter
from aigm.db.base import Base
from aigm.db.models import AdminAuditLog
from aigm.services.game_service import GameService


def test_audit_admin_action_inserts_row() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, future=True)
    Base.metadata.create_all(bind=engine)

    service = GameService(LLMAdapter())
    with Session() as db:
        service.audit_admin_action(
            db,
            actor_source="streamlit",
            actor_id="1",
            actor_display="admin",
            action="test_action",
            target="target",
            metadata={"k": "v"},
        )
        row = db.query(AdminAuditLog).order_by(AdminAuditLog.id.desc()).one()
        assert row.actor_source == "streamlit"
        assert row.actor_id == "1"
        assert row.actor_display == "admin"
        assert row.action == "test_action"
        assert row.target == "target"
        assert row.audit_metadata == {"k": "v"}


def test_admin_audit_log_is_append_only() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, future=True)
    Base.metadata.create_all(bind=engine)

    with Session() as db:
        row = AdminAuditLog(
            actor_source="streamlit",
            actor_id="1",
            actor_display="admin",
            action="original",
            target="t",
            audit_metadata={},
        )
        db.add(row)
        db.commit()

        row.action = "changed"
        with pytest.raises(ValueError, match="append-only"):
            db.commit()
        db.rollback()

        with pytest.raises(ValueError, match="append-only"):
            db.delete(row)
            db.commit()
