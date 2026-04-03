from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aigm.config import settings


if settings.database_url.startswith("sqlite"):
    connect_args = {}
else:
    connect_args = {
        "sslmode": settings.database_sslmode,
        "connect_timeout": settings.database_connect_timeout_s,
    }

engine = create_engine(settings.database_url, future=True, echo=False, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
