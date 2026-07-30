from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    # Lazy on purpose: building this at import time would force full Settings
    # validation (every required env var) just to import app.models — which
    # breaks importing pure helper functions for unit tests, and would also
    # crash the whole app on import if config load order ever changes.
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


def init_db() -> None:
    # metadata.create_all is fine while the schema is still moving. Once this
    # stabilizes past the first few tasks, switch to Alembic migrations instead
    # of hand-editing tables that already hold real history.
    import app.models  # noqa: F401  (registers models on Base.metadata)

    Base.metadata.create_all(bind=get_engine())


def get_db() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
