from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aigm.adapters.llm import LLMAdapter
from aigm.db.base import Base
from aigm.db.models import Campaign, CampaignMemorySummary, TurnLog
from aigm.services.game_service import GameService


def test_refresh_long_term_memory_creates_summary(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, future=True)
    Base.metadata.create_all(bind=engine)

    service = GameService(LLMAdapter())
    with Session() as db:
        campaign = Campaign(discord_thread_id="thread-memory", mode="story", state={"scene": "x", "flags": {}, "party": {}})
        db.add(campaign)
        db.commit()
        for i in range(3):
            db.add(
                TurnLog(
                    campaign_id=campaign.id,
                    actor=f"u{i}",
                    user_input=f"input {i}",
                    ai_raw_output="{}",
                    accepted_commands=[],
                    rejected_commands=[],
                    narration=f"narration {i}",
                    created_at=datetime.utcnow(),
                )
            )
        db.commit()
        monkeypatch.setattr("aigm.services.game_service.settings.context_memory_summary_turns", 3)
        service._refresh_long_term_memory(db, campaign)
        rows = db.query(CampaignMemorySummary).filter(CampaignMemorySummary.campaign_id == campaign.id).all()
        assert len(rows) == 1
        assert "input 0" in rows[0].summary

