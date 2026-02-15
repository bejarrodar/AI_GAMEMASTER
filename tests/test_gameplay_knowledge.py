from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aigm.adapters.llm import LLMAdapter
from aigm.db.base import Base
from aigm.services.game_service import GameService


def test_seed_default_gameplay_knowledge_and_lookup() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, future=True)
    Base.metadata.create_all(bind=engine)
    svc = GameService(LLMAdapter())
    with Session() as db:
        svc.seed_default_gameplay_knowledge(db)
        rulesets = svc.list_game_rulesets(db)
        keys = {r.key for r in rulesets}
        assert "dnd5e-2014" in keys
        assert "story-freeform" in keys

        rows = svc.search_rulebook_entries(db, "advantage d20", ruleset_key="dnd5e-2014", limit=3)
        assert rows
        assert any("advantage" in str(r.get("title", "")).lower() for r in rows)


def test_roll_dice_parsing() -> None:
    svc = GameService(LLMAdapter())
    ok, rolled = svc.roll_dice("2d6+3")
    assert ok
    assert rolled["sides"] == 6
    assert rolled["roll_count"] == 2
    assert len(rolled["rolls"]) == 2
    assert 5 <= rolled["total"] <= 15

    ok_adv, rolled_adv = svc.roll_dice("adv d20+2")
    assert ok_adv
    assert rolled_adv["advantage_mode"] == "advantage"
    assert len(rolled_adv["rolls"]) == 2
    assert rolled_adv["total"] >= 3

    ok_bad, bad = svc.roll_dice("adv 2d20")
    assert not ok_bad
    assert "d20" in str(bad.get("error", "")).lower()
