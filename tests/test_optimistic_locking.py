from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from aigm.db.base import Base
from aigm.db.models import Campaign


def test_campaign_optimistic_locking_conflict() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, future=True)

    with Session() as s:
        c = Campaign(discord_thread_id="thread-1", mode="dnd", state={"scene": "start"})
        s.add(c)
        s.commit()

    s1 = Session()
    s2 = Session()
    try:
        c1 = s1.query(Campaign).filter(Campaign.discord_thread_id == "thread-1").one()
        c2 = s2.query(Campaign).filter(Campaign.discord_thread_id == "thread-1").one()

        c1.state = {"scene": "from s1"}
        s1.commit()

        c2.state = {"scene": "from s2"}
        with pytest.raises(StaleDataError):
            s2.commit()
    finally:
        s1.close()
        s2.close()

