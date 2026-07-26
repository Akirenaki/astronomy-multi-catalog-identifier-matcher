"""Tests for Decision B: cooldown-gated AI summary regeneration.

Covers both the cache-layer function (app.cache.regenerate_ai_summary) and the
route that wraps it (POST /object/{id}/summary/regenerate). Timing is controlled
by writing ai_summary_generated_at directly rather than mocking datetime, since
that's the actual column the cooldown is measured from.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import quote

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cache as cache_mod
from app.database import engine, init_db
from app.main import app
from app.models import Base, ObjectRecord

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    """Same isolation/reset rationale as test_cache.py's fixture: a persistent
    on-disk test database means rows (and this suite's timestamps) would otherwise
    leak between runs."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


async def _resolve_fixture_star(monkeypatch, main_id="* alf Ori", query="Betelgeuse"):
    """Resolve a RESOLVED/PARTIAL object through the normal pipeline so it has real
    identifiers/planets rows, without hitting the network."""
    monkeypatch.setattr(
        "app.resolver.resolve_identity",
        AsyncMock(
            return_value={
                "main_id": main_id,
                "ra": 88.79,
                "dec": 7.41,
                "otype": "Star",
                "sp_type": "M1-M2Ia-Iab",
                "aliases": [query],
            }
        ),
    )
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))
    return await cache_mod.get_or_resolve(query, generate_ai_summary=False)


async def _set_generated_at(main_id: str, when: datetime) -> None:
    """Directly write ai_summary_generated_at to simulate elapsed cooldown time."""
    async with cache_mod.SessionLocal() as session:
        result = await session.execute(select(ObjectRecord).where(ObjectRecord.simbad_main_id == main_id))
        record = result.scalar_one()
        record.ai_summary_generated_at = when
        await session.commit()


@pytest.mark.asyncio
async def test_second_regenerate_within_cooldown_raises(monkeypatch):
    """Cooldown blocks a second regenerate within 5 minutes of the first."""
    await _resolve_fixture_star(monkeypatch)
    summary_mock = AsyncMock(side_effect=["first summary", "second summary"])
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    first = await cache_mod.regenerate_ai_summary("* alf Ori")
    assert first == "first summary"

    with pytest.raises(cache_mod.CooldownActiveError) as exc_info:
        await cache_mod.regenerate_ai_summary("* alf Ori")

    assert 0 < exc_info.value.retry_after_seconds <= 300
    summary_mock.assert_awaited_once()  # second call never reached generate_summary()


@pytest.mark.asyncio
async def test_regenerate_after_cooldown_overwrites_single_row(monkeypatch):
    """Once the cooldown window has passed, regenerate succeeds and overwrites
    the existing summary in place -- one row, not a new one."""
    record = await _resolve_fixture_star(monkeypatch)
    summary_mock = AsyncMock(side_effect=["first summary", "second summary"])
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    first = await cache_mod.regenerate_ai_summary("* alf Ori")
    assert first == "first summary"

    # Simulate 5+ minutes having passed since the first generation.
    await _set_generated_at("* alf Ori", datetime.now(timezone.utc) - timedelta(minutes=6))

    second = await cache_mod.regenerate_ai_summary("* alf Ori")
    assert second == "second summary"
    assert summary_mock.await_count == 2

    async with cache_mod.SessionLocal() as session:
        result = await session.execute(select(ObjectRecord).where(ObjectRecord.simbad_main_id == "* alf Ori"))
        rows = result.scalars().all()

    assert len(rows) == 1
    assert rows[0].ai_summary == "second summary"


@pytest.mark.asyncio
async def test_regenerate_skips_non_resolved_states(monkeypatch):
    """Mirrors store_result()/ensure_ai_summary()'s skip rule: AMBIGUOUS/UNRESOLVED/
    LOOKUP_FAILED objects have no confirmed structured data to regenerate.

    In the real pipeline those states never carry a simbad_main_id (resolver.py
    only sets one for RESOLVED/PARTIAL), so this flips a resolved object's state
    directly in the database to exercise the same skip-rule branch without relying
    on an id that production could never actually pass in.
    """
    await _resolve_fixture_star(monkeypatch)
    async with cache_mod.SessionLocal() as session:
        result = await session.execute(select(ObjectRecord).where(ObjectRecord.simbad_main_id == "* alf Ori"))
        record = result.scalar_one()
        record.resolution_state = "UNRESOLVED"
        await session.commit()

    summary_mock = AsyncMock(return_value="should not be called")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    result = await cache_mod.regenerate_ai_summary("* alf Ori")
    assert result == "No summary available."
    summary_mock.assert_not_awaited()


def test_regenerate_route_returns_429_with_retry_after(monkeypatch):
    """The 429 path is the server-side enforcement the client-side countdown in
    result.html relies on -- it must return a usable Retry-After value."""
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
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="a summary"))

    encoded_id = quote("* alf Ori", safe="")
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.get("/search?q=Betelgeuse")

        first = client.post(f"/object/{encoded_id}/summary/regenerate", headers={"X-CSRF-Token": csrf_token})
        assert first.status_code == 200

        second = client.post(f"/object/{encoded_id}/summary/regenerate", headers={"X-CSRF-Token": csrf_token})

    assert second.status_code == 429
    assert "Retry-After" in second.headers
    retry_after = int(second.headers["Retry-After"])
    assert 0 < retry_after <= 300
    assert second.json()["retry_after_seconds"] == retry_after


def test_regenerate_route_success_includes_summary_html(monkeypatch):
    """Regression test for F1: the regenerate route's success response must include
    summary_html (matching the shape returned by the non-regenerate /summary route),
    since result.html's wireRegenerateButton() JS reads data.summary_html and falls
    back to a "No summary available." placeholder if it's missing."""
    from app.narrative import render_summary_markdown

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
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="A regenerated summary."))

    encoded_id = quote("* alf Ori", safe="")
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.get("/search?q=Betelgeuse")

        response = client.post(f"/object/{encoded_id}/summary/regenerate", headers={"X-CSRF-Token": csrf_token})

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "A regenerated summary."
    assert "summary_html" in body
    assert body["summary_html"] == render_summary_markdown(body["summary"])
    assert "cooldown_seconds" in body
