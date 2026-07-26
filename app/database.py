"""Database setup."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import load_environment
from app.models import Base

load_environment()

_raw_database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./astronomy.db")
_connect_args: dict = {}
if _raw_database_url.startswith("postgresql://") or _raw_database_url.startswith("postgres://"):
    DATABASE_URL = make_url(_raw_database_url).set(drivername="postgresql+asyncpg", query={})
    _connect_args = {"ssl": "require"}
else:
    DATABASE_URL = _raw_database_url

engine = create_async_engine(DATABASE_URL, echo=False, connect_args=_connect_args)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    """Enable SQLite foreign-key enforcement (including ON DELETE CASCADE) so
    dev/CI SQLite behaves the same way production Postgres already does by
    default. SQLite does not enforce foreign keys unless this pragma is issued
    per connection, so without it, deleting a parent row (e.g. an ObjectRecord)
    would silently leave dangling child rows in SQLite while Postgres would
    cascade-delete them -- a dev/prod behavioural divergence.

    Strictly guarded on dialect name: issuing a SQLite PRAGMA against a
    Postgres connection would error.
    """
    if engine.dialect.name == "sqlite":
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def init_db() -> None:
    """Create the database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def reset_db() -> None:
    """Reset the database schema."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session."""
    async with SessionLocal() as session:
        yield session
