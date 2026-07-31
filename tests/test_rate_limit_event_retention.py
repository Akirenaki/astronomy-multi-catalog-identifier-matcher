"""Regression test for F6: rate_limit_events retention cleanup."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.database import SessionLocal, engine, init_db
from app.models import Base, RateLimitEvent
from app.ratelimit import purge_old_rate_limit_events


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


@pytest.mark.asyncio
async def test_purge_old_rate_limit_events_deletes_only_stale_rows():
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        session.add_all(
            [
                RateLimitEvent(subject_type="auth", subject_id="s1", created_at=now - timedelta(hours=48)),
                RateLimitEvent(subject_type="auth", subject_id="s1", created_at=now - timedelta(hours=25)),
                RateLimitEvent(subject_type="auth", subject_id="s1", created_at=now - timedelta(hours=1)),
            ]
        )
        await session.commit()

    deleted = await purge_old_rate_limit_events(older_than=timedelta(hours=24))
    assert deleted == 2

    async with SessionLocal() as session:
        from sqlalchemy import select

        remaining = (await session.execute(select(RateLimitEvent))).scalars().all()
        assert len(remaining) == 1
