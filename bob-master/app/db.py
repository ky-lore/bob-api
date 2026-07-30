import re
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Enum as SAEnum
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


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
    _sync_postgres_enum_values()


def _sync_postgres_enum_values() -> None:
    """create_all only creates enum TYPES that don't exist yet — it never adds
    new values to one that already exists. Confirmed the hard way: adding
    ManagedListType.alias in Python never touched the real Postgres type,
    and every query filtering on it 500'd with "invalid input value for enum
    managedlisttype". Runs on every startup; each ADD VALUE IF NOT EXISTS is a
    cheap no-op once the value's already there. No-op entirely on SQLite,
    which has no native enum type (Enum columns are just a CHECK constraint)."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if not isinstance(column.type, SAEnum) or not column.type.name:
                    continue
                type_name = column.type.name
                if not _SAFE_IDENTIFIER_RE.match(type_name):
                    continue
                for value in column.type.enums:
                    if not _SAFE_IDENTIFIER_RE.match(value):
                        continue
                    conn.execute(text(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{value}'"))


def get_db() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
