"""Regression test for F4: alembic/env.py must build its migration engine
with the same connect_args (e.g. ssl=require for Postgres) that the app's
own engine uses for the same URL, or `alembic upgrade head` silently drops
TLS against managed Postgres providers that require it. Not testable
end-to-end without a live TLS-requiring Postgres instance (see audit), so
this asserts the shared derivation directly, and that alembic/env.py is
wired up to use it.
"""

from app.database import get_connect_args


def test_get_connect_args_requires_ssl_for_postgres_urls():
    assert get_connect_args("postgresql://user:pass@host/db") == {"ssl": "require"}
    assert get_connect_args("postgres://user:pass@host/db") == {"ssl": "require"}
    assert get_connect_args("postgresql+asyncpg://user:pass@host/db") == {"ssl": "require"}


def test_get_connect_args_is_empty_for_sqlite_urls():
    assert get_connect_args("sqlite+aiosqlite:///./astronomy.db") == {}


def test_alembic_env_uses_get_connect_args_for_its_engine():
    """Guards against alembic/env.py's async_engine_from_config() call
    silently losing its connect_args=get_connect_args(...) wiring in a
    future edit (there's no live Postgres to run an end-to-end check
    against in this environment, so this pins the source-level contract)."""
    from pathlib import Path

    env_py = Path(__file__).resolve().parent.parent / "alembic" / "env.py"
    source = env_py.read_text()
    assert "get_connect_args" in source
    assert "connect_args=get_connect_args(" in source
