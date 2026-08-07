"""Database engine, session, and schema helpers."""

import os
from collections.abc import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import load_environment
from app.models import Base

load_environment()

def get_connect_args(url: str) -> dict:
    """Return the connect_args this app uses for a given database URL.

    Postgres URLs require TLS on essentially every managed provider this
    project targets (Neon, Render Postgres, Supabase, ...); asyncpg does not
    enable TLS on its own. Factored out so alembic/env.py can build its own
    engine with the exact same connect_args instead of silently connecting
    without TLS.
    """
    if url.startswith("postgresql://") or url.startswith("postgres://") or url.startswith("postgresql+asyncpg://"):
        return {"ssl": "require"}
    return {}


_raw_database_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./astronomy.db")
_connect_args = get_connect_args(_raw_database_url)
if _raw_database_url.startswith("postgresql://") or _raw_database_url.startswith("postgres://"):
    DATABASE_URL = make_url(_raw_database_url).set(drivername="postgresql+asyncpg", query={})
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
    """Create application tables for first-run and test setup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def reset_db() -> None:
    """Drop and recreate all tables for a clean database state."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a scoped async session for request handlers and services."""
    async with SessionLocal() as session:
        yield session
