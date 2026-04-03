from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aigm.adapters.llm import LLMAdapter
from aigm.db.base import Base
from aigm.services.game_service import GameService


def test_auth_seed_and_permission_flow() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    service = GameService(LLMAdapter())
    with TestingSession() as db:
        service.seed_default_auth(db)
        ok, msg = service.auth_create_user(
            db,
            username="tester",
            password="secret123",
            display_name="Tester",
            roles=["gm"],
        )
        assert ok, msg
        user = service.auth_authenticate_user(db, "tester", "secret123")
        assert user is not None
        assert service.auth_user_has_permission(db, user.id, "campaign.play")
        assert service.auth_user_has_permission(db, user.id, "campaign.write")
        assert not service.auth_user_has_permission(db, user.id, "system.admin")
