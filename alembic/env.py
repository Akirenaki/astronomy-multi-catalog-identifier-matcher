"""Alembic environment.

Deliberately reuses app.database's engine (which itself calls
app.config.load_environment() and applies the same DATABASE_URL /
Postgres-URL-rewriting logic the running application uses), so
`alembic upgrade head` / `alembic revision --autogenerate` always
operate against the exact same database the app would connect to --
no separate URL configuration to keep in sync.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.database import engine as app_engine
from app.database import get_connect_args
from app.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Reuse the app's own SQLAlchemy metadata so --autogenerate compares
# against the real, current models rather than a separately-maintained copy.
target_metadata = Base.metadata

# Reuse the app's already-constructed async engine URL (post Postgres
# rewriting / .env loading) instead of alembic.ini's static sqlalchemy.url.
config.set_main_option("sqlalchemy.url", str(app_engine.url).replace("%", "%%"))


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no live DB connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async engine, mirroring how
    the application itself connects (aiosqlite / asyncpg)."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=get_connect_args(str(app_engine.url)),
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
