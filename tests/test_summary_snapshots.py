"""Tests for Decision F's user_summary_snapshots: generating/regenerating as a
logged-in user must create a personal snapshot in addition to (never instead of)
updating the shared canonical ObjectRecord.ai_summary. The last test in this file
is the regression guard explicitly called for in the handoff doc: it would catch an
accidental slide back into per-user fragmentation of the shared summary."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cache as cache_mod
from app.database import engine, init_db
from app.main import app
from app.models import Base, ObjectRecord, UserSummarySnapshot

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def _mock_betelgeuse(monkeypatch):
    monkeypatch.setattr(
        "app.resolver.resolve_identity",
        AsyncMock(
            return_value={
                "main_id": "* alf Ori",
                "ra": 88.79,
                "dec": 7.41,
                "otype": "Star",
                "sp_type": "M1-M2Ia-Iab",
                "aliases": ["Betelgeuse"],
            }
        ),
    )
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))


def test_generate_as_logged_in_user_creates_snapshot_row(monkeypatch):
    """GET /object/{id}/summary while logged in must create a
    user_summary_snapshots row with the generated text."""
    _mock_betelgeuse(monkeypatch)
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="A red supergiant."))
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post(
            "/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token}
        )
        client.get("/search?q=Betelgeuse")
        client.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )

    async def _fetch():
        async with cache_mod.SessionLocal() as session:
            result = await session.execute(select(UserSummarySnapshot))
            return result.scalars().all()

    import asyncio

    rows = asyncio.run(_fetch())
    assert len(rows) == 1
    assert rows[0].summary_text == "A red supergiant."


def test_generate_as_anonymous_user_creates_no_snapshot(monkeypatch):
    """The same action performed anonymously must not create a snapshot row --
    there's no user_id to attach one to."""
    _mock_betelgeuse(monkeypatch)
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="A red supergiant."))
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.get("/search?q=Betelgeuse")
        client.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )

    async def _fetch():
        async with cache_mod.SessionLocal() as session:
            result = await session.execute(select(UserSummarySnapshot))
            return result.scalars().all()

    import asyncio

    rows = asyncio.run(_fetch())
    assert rows == []


def test_account_saved_shows_personal_snapshot_not_someone_elses_regenerate(monkeypatch):
    """Core ownership guarantee: user A generates a summary and favorites the
    object; user B (also logged in) regenerates it later. User A's /account/saved
    view must still show what A themselves generated, unaffected by B's action."""
    _mock_betelgeuse(monkeypatch)
    summary_mock = AsyncMock(side_effect=["A's summary", "B's summary"])
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client_a, TestClient(app) as client_b:
        csrf_token_a = get_csrf_token(client_a)
        client_a.post(
            "/register", data={"email": "user-a@example.com", "password": "hunter22", "csrf_token": csrf_token_a}
        )
        client_a.get("/search?q=Betelgeuse")
        client_a.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": csrf_token_a},
        )  # A generates "A's summary"
        client_a.post(f"/object/{encoded_id}/favorite", data={"csrf_token": csrf_token_a})

        # Simulate the cooldown having elapsed so B's regenerate isn't blocked by
        # Decision B's per-object cooldown.
        async def _age_cooldown():
            async with cache_mod.SessionLocal() as session:
                result = await session.execute(
                    select(ObjectRecord).where(ObjectRecord.simbad_main_id == "* alf Ori")
                )
                record = result.scalar_one()
                record.ai_summary_generated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
                await session.commit()

        import asyncio

        asyncio.run(_age_cooldown())

        csrf_token_b = get_csrf_token(client_b)
        client_b.post(
            "/register", data={"email": "user-b@example.com", "password": "hunter22", "csrf_token": csrf_token_b}
        )
        client_b.post(
            f"/object/{encoded_id}/summary/regenerate", headers={"X-CSRF-Token": csrf_token_b}
        )  # B regenerates -> "B's summary"

        a_saved_page = client_a.get("/account/saved")

    assert "A's summary" in a_saved_page.text
    assert "B's summary" not in a_saved_page.text


def test_ai_summary_remains_single_global_value_regardless_of_snapshot_count(monkeypatch):
    """Regression guard called for explicitly in the handoff doc: ObjectRecord.
    ai_summary must remain one shared value no matter how many different users
    have their own personal snapshots for the same object -- this is the test
    that would catch an accidental slide back into per-user fragmentation."""
    _mock_betelgeuse(monkeypatch)
    summary_mock = AsyncMock(side_effect=["first summary", "second summary", "third summary"])
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client_a, TestClient(app) as client_b, TestClient(app) as client_c:
        csrf_token_a = get_csrf_token(client_a)
        client_a.post(
            "/register", data={"email": "user-a2@example.com", "password": "hunter22", "csrf_token": csrf_token_a}
        )
        client_a.get("/search?q=Betelgeuse")
        client_a.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": csrf_token_a},
        )  # generates the ONE shared summary

        csrf_token_b = get_csrf_token(client_b)
        client_b.post(
            "/register", data={"email": "user-b2@example.com", "password": "hunter22", "csrf_token": csrf_token_b}
        )
        client_b.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": csrf_token_b},
        )  # cache hit, no new Gemini call

        csrf_token_c = get_csrf_token(client_c)
        client_c.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": csrf_token_c},
        )  # anonymous cache hit

    async def _fetch():
        async with cache_mod.SessionLocal() as session:
            objects_result = await session.execute(select(ObjectRecord))
            objects = objects_result.scalars().all()
            snapshots_result = await session.execute(select(UserSummarySnapshot))
            snapshots = snapshots_result.scalars().all()
            return objects, snapshots

    import asyncio

    objects, snapshots = asyncio.run(_fetch())

    assert len(objects) == 1
    assert objects[0].ai_summary == "first summary"
    # Two logged-in users hit this endpoint; the ai_summary generation itself only
    # ran once (cache hit for user B), but ensure_ai_summary() only saves a snapshot
    # for the requesting user each time it's called, regardless of whether Gemini
    # was actually re-invoked -- so both A and B get their own snapshot row, each
    # pointing at the one shared summary text, and there is still exactly one
    # ObjectRecord.ai_summary value overall.
    assert len(snapshots) == 2
    assert {s.summary_text for s in snapshots} == {"first summary"}
