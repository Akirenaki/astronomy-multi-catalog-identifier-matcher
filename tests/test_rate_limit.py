"""Tests for Decision D: per-client rate limiting on AI-summary generation, layered
on top of (not replacing) Decision B's per-object cooldown. See app/ratelimit.py's
module docstring for why both layers exist."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from datetime import timedelta
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app import ratelimit as ratelimit_mod
from app.database import engine, init_db
from app.main import app
from app.models import Base

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


# --- unit tests on the ratelimit module directly ------------------------------

@pytest.mark.asyncio
async def test_check_and_record_allows_requests_under_the_limit():
    for _ in range(3):
        await ratelimit_mod.check_and_record("session", "abc", limit=3, window=timedelta(hours=1))


@pytest.mark.asyncio
async def test_check_and_record_blocks_once_limit_reached():
    for _ in range(3):
        await ratelimit_mod.check_and_record("session", "abc", limit=3, window=timedelta(hours=1))

    with pytest.raises(ratelimit_mod.RateLimitExceededError) as exc_info:
        await ratelimit_mod.check_and_record("session", "abc", limit=3, window=timedelta(hours=1))

    assert exc_info.value.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_check_and_record_tracks_subjects_independently():
    """Two different subjects (e.g. two different sessions, or a session and a
    user) must not share the same limit bucket."""
    for _ in range(3):
        await ratelimit_mod.check_and_record("session", "subject-1", limit=3, window=timedelta(hours=1))

    # subject-2 should be unaffected by subject-1 hitting its limit.
    await ratelimit_mod.check_and_record("session", "subject-2", limit=3, window=timedelta(hours=1))


@pytest.mark.asyncio
async def test_check_and_record_user_and_session_subject_types_are_independent():
    """A 'user' subject and a 'session' subject with the same id string must be
    tracked separately (the subject_type column is part of the key)."""
    for _ in range(3):
        await ratelimit_mod.check_and_record("session", "42", limit=3, window=timedelta(hours=1))

    # subject_type='user', subject_id='42' is a different bucket entirely.
    await ratelimit_mod.check_and_record("user", "42", limit=3, window=timedelta(hours=1))


# --- route-level integration test ---------------------------------------------

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


def test_anonymous_client_is_rate_limited_across_different_objects(monkeypatch):
    """Decision D's core scenario, taken straight from the doc's own framing of the
    gap: one anonymous client clicking Generate across MANY DIFFERENT objects must
    eventually be rate limited, even though Decision B's per-object cooldown never
    fires (each object is only hit once)."""
    monkeypatch.setattr(ratelimit_mod, "AI_SUMMARY_RATE_LIMIT", 2)
    _mock_betelgeuse(monkeypatch)
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="a summary"))

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        # Object 1
        client.get("/search?q=Betelgeuse")
        r1 = client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r1.status_code == 200

        # Object 2 (a different query_text/main_id, same client/session)
        monkeypatch.setattr(
            "app.resolver.resolve_identity",
            AsyncMock(
                return_value={
                    "main_id": "51 Peg",
                    "ra": 344.36,
                    "dec": 20.77,
                    "otype": "Star",
                    "sp_type": "G5V",
                    "aliases": ["51 Pegasi"],
                }
            ),
        )
        client.get("/search?q=51+Pegasi")
        r2 = client.post(
            f"/object/{quote('51 Peg', safe='')}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r2.status_code == 200

        # Object 3 -- this is the third Gemini-quota-spending request from the same
        # client within the window, and the limit was set to 2.
        monkeypatch.setattr(
            "app.resolver.resolve_identity",
            AsyncMock(
                return_value={
                    "main_id": "Proxima Centauri",
                    "ra": 217.39,
                    "dec": -62.68,
                    "otype": "Star",
                    "sp_type": "M5.5Ve",
                    "aliases": ["Proxima Cen"],
                }
            ),
        )
        client.get("/search?q=Proxima+Cen")
        r3 = client.post(
            f"/object/{quote('Proxima Centauri', safe='')}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert r3.status_code == 429
    assert "Retry-After" in r3.headers
    assert r3.json()["retry_after_seconds"] > 0


def test_rate_limit_response_shape_matches_cooldown_response_shape(monkeypatch):
    """The existing client-side JS in result.html was written to parse
    {retry_after_seconds} out of a 429 body for the cooldown case (Decision B).
    The rate-limit 429 (Decision D) must return the same shape so that same
    handling code works for both without the frontend needing to distinguish them."""
    monkeypatch.setattr(ratelimit_mod, "AI_SUMMARY_RATE_LIMIT", 1)
    _mock_betelgeuse(monkeypatch)
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="a summary"))

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.get("/search?q=Betelgeuse")
        client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary",
            headers={"X-CSRF-Token": csrf_token},
        )  # consumes the only slot

        blocked = client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary/regenerate",
            headers={"X-CSRF-Token": csrf_token},
        )

    assert blocked.status_code == 429
    body = blocked.json()
    assert "retry_after_seconds" in body
    assert isinstance(body["retry_after_seconds"], int)
