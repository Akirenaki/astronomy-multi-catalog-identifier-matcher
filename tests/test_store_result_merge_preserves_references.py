"""Regression tests for F4/TICKET-E: when store_result() merges two
pre-existing candidate rows (one matched by query_text, one matched by
simbad_main_id) into a single surviving row, any SavedSearch,
UserSummarySnapshot, or QueryAlias row that referenced the row about to become
"stale" must keep working -- re-pointed onto the surviving primary_row -- not
silently orphaned (pre-TICKET-D SQLite) or cascade-deleted out from under the
user (Postgres, and SQLite after TICKET-D).
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from sqlalchemy import select

from app import cache as cache_mod
from app.auth import create_user
from app.database import SessionLocal, engine, init_db
from app.models import Base, ObjectRecord, QueryAlias, SavedSearch, UserSummarySnapshot
from app.resolver import ResolutionResult


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def _resolution_result() -> ResolutionResult:
    """A resolution that will collide the two rows created in each test below:
    query_text matches one pre-existing row, main_id matches the other."""
    return ResolutionResult(
        query_text="51 Peg",
        state="RESOLVED",
        main_id="51 Peg",
        ra=344.36,
        dec=20.77,
        otype="Star",
        spectral_type="G2V",
    )


async def _make_colliding_rows() -> tuple[int, int]:
    """Create two separate ObjectRecord rows that store_result() will treat as
    candidates for the same object: one reachable only by query_text ("51
    Peg"), one reachable only by simbad_main_id ("51 Peg"). Returns
    (query_text_row_id, main_id_row_id). store_result() picks the query_text
    match as primary_row and the main_id match as the stale row (see
    store_result()'s candidate_rows construction order)."""
    now = datetime.now(timezone.utc)
    async with SessionLocal() as session:
        query_text_row = ObjectRecord(
            simbad_main_id=None,
            query_text="51 Peg",
            resolution_state="UNRESOLVED",
            expires_at=now + timedelta(hours=1),
        )
        main_id_row = ObjectRecord(
            simbad_main_id="51 Peg",
            query_text="51 Pegasi (old alias)",
            resolution_state="RESOLVED",
            expires_at=now + timedelta(days=14),
        )
        session.add_all([query_text_row, main_id_row])
        await session.commit()
        return query_text_row.id, main_id_row.id


@pytest.mark.asyncio
async def test_merge_repoints_saved_search_onto_surviving_row():
    """A SavedSearch attached to the row that becomes stale must survive the
    merge, pointing at the surviving primary_row."""
    query_text_row_id, main_id_row_id = await _make_colliding_rows()
    user = await create_user("wolfie@example.com", "hunter22")

    async with SessionLocal() as session:
        session.add(SavedSearch(user_id=user.id, object_id=main_id_row_id))
        await session.commit()

    record = await cache_mod.store_result(_resolution_result(), generate_ai_summary=False)

    async with SessionLocal() as session:
        saved = (
            (await session.execute(select(SavedSearch).where(SavedSearch.user_id == user.id))).scalars().all()
        )

    assert len(saved) == 1
    assert saved[0].object_id == record.id
    assert record.id == query_text_row_id  # primary_row is the query_text match


@pytest.mark.asyncio
async def test_merge_repoints_summary_snapshot_onto_surviving_row():
    """A UserSummarySnapshot attached to the row that becomes stale must
    survive the merge, pointing at the surviving primary_row."""
    query_text_row_id, main_id_row_id = await _make_colliding_rows()
    user = await create_user("wolfie@example.com", "hunter22")

    async with SessionLocal() as session:
        session.add(
            UserSummarySnapshot(user_id=user.id, object_id=main_id_row_id, summary_text="A saved snapshot.")
        )
        await session.commit()

    record = await cache_mod.store_result(_resolution_result(), generate_ai_summary=False)

    async with SessionLocal() as session:
        snapshots = (
            (await session.execute(select(UserSummarySnapshot).where(UserSummarySnapshot.user_id == user.id)))
            .scalars()
            .all()
        )

    assert len(snapshots) == 1
    assert snapshots[0].object_id == record.id
    assert snapshots[0].summary_text == "A saved snapshot."


@pytest.mark.asyncio
async def test_merge_repoints_query_alias_onto_surviving_row():
    """A QueryAlias pointing at the row that becomes stale must survive the
    merge, pointing at the surviving primary_row."""
    query_text_row_id, main_id_row_id = await _make_colliding_rows()

    async with SessionLocal() as session:
        session.add(QueryAlias(query_text="51 Pegasi", object_id=main_id_row_id))
        await session.commit()

    record = await cache_mod.store_result(_resolution_result(), generate_ai_summary=False)

    async with SessionLocal() as session:
        alias = (
            await session.execute(select(QueryAlias).where(QueryAlias.query_text == "51 Pegasi"))
        ).scalar_one()

    assert alias.object_id == record.id


@pytest.mark.asyncio
async def test_merge_with_duplicate_favorite_on_both_rows_does_not_raise():
    """Edge case: the same user already has a SavedSearch on *both* the row
    that will survive and the row that will become stale. Re-pointing the
    stale row's SavedSearch onto primary_row would violate the
    UniqueConstraint("user_id", "object_id") -- store_result() must detect
    this and drop the stale duplicate instead of raising IntegrityError."""
    query_text_row_id, main_id_row_id = await _make_colliding_rows()
    user = await create_user("wolfie@example.com", "hunter22")

    async with SessionLocal() as session:
        session.add(SavedSearch(user_id=user.id, object_id=query_text_row_id))
        session.add(SavedSearch(user_id=user.id, object_id=main_id_row_id))
        await session.commit()

    # Must not raise IntegrityError.
    record = await cache_mod.store_result(_resolution_result(), generate_ai_summary=False)

    async with SessionLocal() as session:
        saved = (
            (await session.execute(select(SavedSearch).where(SavedSearch.user_id == user.id))).scalars().all()
        )

    # No duplicate: exactly one SavedSearch remains, pointing at the surviving row.
    assert len(saved) == 1
    assert saved[0].object_id == record.id
