"""Tests for F2/TICKET-2: app.main.lifespan() no longer unconditionally calls
init_db()/create_all() on every startup. It gates that behind an explicit
DEV_AUTO_CREATE_SCHEMA=1 opt-in; otherwise it does a lightweight read-only
check that the schema already exists (e.g. via Alembic) and fails startup
loudly if it doesn't, rather than silently creating whatever tables happen
to be missing.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.database import engine, init_db
from app.main import app, lifespan
from app.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _clean_db():
    """Start each test from a genuinely empty (no-tables) database, since
    these tests specifically exercise what lifespan() does when the schema
    is/isn't already present."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.asyncio
async def test_lifespan_fails_fast_when_schema_missing_and_auto_create_disabled(monkeypatch):
    monkeypatch.delenv("DEV_AUTO_CREATE_SCHEMA", raising=False)
    with pytest.raises(RuntimeError, match="Database schema check failed"):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_lifespan_succeeds_when_schema_already_present(monkeypatch):
    monkeypatch.delenv("DEV_AUTO_CREATE_SCHEMA", raising=False)
    await init_db()  # simulate Alembic having already created the schema
    async with lifespan(app):
        pass  # no RuntimeError


@pytest.mark.asyncio
async def test_lifespan_creates_schema_when_dev_auto_create_enabled(monkeypatch):
    monkeypatch.setenv("DEV_AUTO_CREATE_SCHEMA", "1")
    async with lifespan(app):
        pass
    async with engine.begin() as conn:
        # A trivial query against a known table succeeds only if create_all() ran.
        await conn.execute(__import__("sqlalchemy").text("SELECT 1 FROM users LIMIT 1"))


def test_testclient_boots_normally_when_fixture_precreates_schema():
    """End-to-end sanity check: the app's own TestClient usage pattern (a
    test fixture creates tables directly, then `with TestClient(app)` runs
    lifespan) still works with no env var set, exactly as every other test
    module in this suite relies on."""
    import asyncio

    asyncio.run(init_db())
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
