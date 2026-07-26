"""Tests for Gemini error-handling/rate-limiting behaviour:

- A failed Gemini generation call must not start the per-object regenerate
  cooldown (app.cache.regenerate_ai_summary should leave ai_summary_generated_at
  untouched).
- A failed Gemini generation call must not write a rate_limit_events row (the
  route-level check_limit()/record_usage() split in app.main).
- The route surfaces a distinct 503 with a real message instead of silently
  returning 200 with a placeholder string.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import quote

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import cache as cache_mod
from app.database import engine, init_db
from app.main import app
from app.models import Base, ObjectRecord, RateLimitEvent
from app.narrative import GeminiGenerationError, GeminiRateLimitedError

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


async def _resolve_fixture_star(monkeypatch, main_id="* alf Ori", query="Betelgeuse"):
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


async def _rate_limit_event_count() -> int:
    async with cache_mod.SessionLocal() as session:
        result = await session.execute(select(func.count()).select_from(RateLimitEvent))
        return result.scalar_one()


# --- cache-layer: no cooldown penalty on a failed generation -------------------

@pytest.mark.asyncio
async def test_regenerate_does_not_start_cooldown_on_gemini_failure(monkeypatch):
    """A failed generate_summary() call must leave ai_summary_generated_at (the
    cooldown clock) untouched, so the user can retry immediately."""
    await _resolve_fixture_star(monkeypatch)
    monkeypatch.setattr(
        "app.cache.generate_summary",
        AsyncMock(side_effect=GeminiGenerationError("boom")),
    )

    with pytest.raises(GeminiGenerationError):
        await cache_mod.regenerate_ai_summary("* alf Ori")

    async with cache_mod.SessionLocal() as session:
        result = await session.execute(select(ObjectRecord).where(ObjectRecord.simbad_main_id == "* alf Ori"))
        record = result.scalar_one()
        assert record.ai_summary_generated_at is None
        assert not record.ai_summary

    # Immediately retrying (no cooldown was started) should be allowed to reach
    # generate_summary() again rather than raising CooldownActiveError.
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="a real summary"))
    summary = await cache_mod.regenerate_ai_summary("* alf Ori")
    assert summary == "a real summary"


@pytest.mark.asyncio
async def test_ensure_ai_summary_propagates_gemini_failure_without_caching(monkeypatch):
    """ensure_ai_summary() must not cache a failed attempt as record.ai_summary --
    otherwise every future page view/poll would keep serving the failure."""
    await _resolve_fixture_star(monkeypatch)
    monkeypatch.setattr(
        "app.cache.generate_summary",
        AsyncMock(side_effect=GeminiRateLimitedError("Gemini is busy")),
    )

    with pytest.raises(GeminiRateLimitedError):
        await cache_mod.ensure_ai_summary("* alf Ori")

    async with cache_mod.SessionLocal() as session:
        result = await session.execute(select(ObjectRecord).where(ObjectRecord.simbad_main_id == "* alf Ori"))
        record = result.scalar_one()
        assert not record.ai_summary


# --- route-level: 503 shape, no rate_limit_events row on failure ---------------

def test_generate_route_returns_503_with_message_on_gemini_failure(monkeypatch):
    with TestClient(app) as client:
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
        client.get("/search?q=Betelgeuse")

        monkeypatch.setattr(
            "app.cache.generate_summary",
            AsyncMock(side_effect=GeminiGenerationError("The AI summary service returned an error.")),
        )

        response = client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary",
            headers={"X-CSRF-Token": get_csrf_token(client)},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "ai_generation_failed"
    assert "message" in body and body["message"]


def test_generate_route_does_not_record_rate_limit_usage_on_failure(monkeypatch):
    """The core fix: a failed Gemini call must not write a rate_limit_events row.
    Verified by exhausting a tiny limit with failing calls and confirming a
    subsequent call is still allowed through (not 429)."""
    monkeypatch.setattr("app.ratelimit.AI_SUMMARY_RATE_LIMIT", 1)

    with TestClient(app) as client:
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
        client.get("/search?q=Betelgeuse")

        monkeypatch.setattr(
            "app.cache.generate_summary",
            AsyncMock(side_effect=GeminiGenerationError("fail 1")),
        )
        csrf_token = get_csrf_token(client)
        r1 = client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r1.status_code == 503

        # limit=1 would reject this as a second attempt if the failed call above
        # had recorded usage; it must not have.
        monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="a real summary"))
        r2 = client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert r2.status_code == 200
    assert r2.json()["summary"] == "a real summary"


@pytest.mark.asyncio
async def test_record_usage_writes_exactly_one_row():
    from app.ratelimit import record_usage

    assert await _rate_limit_event_count() == 0
    await record_usage("session", "abc")
    assert await _rate_limit_event_count() == 1
