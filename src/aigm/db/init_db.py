from aigm.db.base import Base
from aigm.db.session import engine
from aigm.db import models  # noqa: F401 - register model metadata


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db()
