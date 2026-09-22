"""SQLite (WAL) session factory — ADR 0002.

Single writer (the engine), many readers.  Schema creation is idempotent via
SQLAlchemy metadata plus a ``schema_version`` row; Alembic is introduced when
Phase 3 first *changes* the schema, so migration history starts from a real
migration rather than a synthetic initial one.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import SCHEMA_VERSION, Base, SchemaVersion

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


def create_db_engine(data_dir: Path, *, filename: str = "engine.db") -> Engine:
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{data_dir / filename}", future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        for pragma in _PRAGMAS:
            cursor.execute(pragma)
        cursor.close()

    return engine


class SchemaMismatch(RuntimeError):
    pass


def init_schema(engine: Engine) -> None:
    """Create tables if absent; refuse to run against a different version."""
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        row = session.get(SchemaVersion, 1)
        if row is None:
            session.add(SchemaVersion(id=1, version=SCHEMA_VERSION))
            session.commit()
        elif row.version != SCHEMA_VERSION:
            raise SchemaMismatch(
                f"data store is schema {row.version}, code expects {SCHEMA_VERSION}; "
                "run the migration before starting the engine")


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False, future=True)


def wal_checkpoint(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
