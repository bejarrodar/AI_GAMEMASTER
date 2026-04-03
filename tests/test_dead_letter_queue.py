from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aigm.db.base import Base
from aigm.db.models import Campaign
from aigm.services.game_service import GameService


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


class _LLMStub:
    pass


def test_dead_letter_enqueue_and_list() -> None:
    db = _session()
    svc = GameService(_LLMStub())  # type: ignore[arg-type]
    campaign = Campaign(discord_thread_id="thread-1", mode="dnd", state={"scene": "x"})
    db.add(campaign)
    db.commit()
    db.refresh(campaign)

    event_id = svc.enqueue_dead_letter_event(
        db,
        event_type="turn_job",
        campaign_id=campaign.id,
        discord_thread_id=campaign.discord_thread_id,
        discord_message_id="m1",
        actor_discord_user_id="u1",
        actor_display_name="Tester",
        user_input="I swing my sword.",
        error_message="queue_full",
        payload={"source": "unit_test"},
    )
    assert int(event_id or 0) > 0
    rows = svc.list_dead_letter_events(db, status="open", limit=10)
    assert len(rows) == 1
    assert rows[0]["discord_message_id"] == "m1"
    assert rows[0]["status"] == "open"


def test_dead_letter_replay_marks_replayed(monkeypatch) -> None:
    db = _session()
    svc = GameService(_LLMStub())  # type: ignore[arg-type]
    campaign = Campaign(discord_thread_id="thread-2", mode="dnd", state={"scene": "x"})
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    event_id = svc.enqueue_dead_letter_event(
        db,
        event_type="turn_job",
        campaign_id=campaign.id,
        discord_thread_id=campaign.discord_thread_id,
        actor_discord_user_id="u2",
        actor_display_name="ReplayUser",
        user_input="I look around.",
        error_message="worker_crash",
    )
    assert event_id is not None

    def _fake_process_turn_routed(db_arg, campaign, actor, actor_display_name, user_input):  # noqa: ARG001
        return "Narration", {"accepted": [], "rejected": []}

    monkeypatch.setattr(svc, "process_turn_routed", _fake_process_turn_routed)
    ok, detail, row = svc.replay_dead_letter_event(db, event_id=int(event_id))
    assert ok is True
    assert "completed" in detail.lower()
    assert row["status"] == "replayed"
