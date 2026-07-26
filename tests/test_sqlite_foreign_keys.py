"""Regression test for TICKET-D: SQLite must enforce foreign keys (including
ON DELETE CASCADE), matching production Postgres's default behaviour, so
dev/CI doesn't silently diverge from production when a parent row is deleted.
"""

import os
from datetime import datetime, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth import create_user
from app.database import SessionLocal, engine, init_db
from app.models import Base, ObjectRecord, SavedSearch


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


@pytest.mark.asyncio
async def test_deleting_object_cascades_to_saved_search():
    """With PRAGMA foreign_keys=ON active, deleting an ObjectRecord that has an
    attached SavedSearch must cascade-delete the SavedSearch row too (matching
    Postgres's ON DELETE CASCADE), rather than leaving it dangling."""
    user = await create_user("wolfie@example.com", "hunter22")

    async with SessionLocal() as session:
        obj = ObjectRecord(
            simbad_main_id="* alf Ori",
            query_text="Betelgeuse",
            resolution_state="RESOLVED",
            expires_at=datetime.now(timezone.utc),
        )
        session.add(obj)
        await session.flush()

        saved = SavedSearch(user_id=user.id, object_id=obj.id, created_at=datetime.now(timezone.utc))
        session.add(saved)
        await session.commit()

        object_id = obj.id

    async with SessionLocal() as session:
        obj_to_delete = await session.get(ObjectRecord, object_id)
        await session.delete(obj_to_delete)
        await session.commit()

    async with SessionLocal() as session:
        remaining = (await session.execute(select(SavedSearch).where(SavedSearch.object_id == object_id))).scalars().all()

    assert remaining == []
