"""Tests for rate limiting and length-bounding on /search and /api/resolve
(F3 / F13 / TICKET-02)."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from unittest.mock import AsyncMock

import pytest_asyncio
from fastapi.testclient import TestClient

from app import main as main_mod
from app.database import engine, init_db
from app.main import app
from app.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def _mock_simbad(monkeypatch, call_counter):
    async def fake_resolve_identity(*args, **kwargs):
        call_counter["count"] += 1
        return {
            "main_id": "* alf Ori",
            "ra": 88.79,
            "dec": 7.41,
            "otype": "Star",
            "sp_type": "M1-M2Ia-Iab",
            "aliases": ["Betelgeuse"],
        }

    monkeypatch.setattr("app.resolver.resolve_identity", fake_resolve_identity)
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))
    # main.py's /api/resolve route explicitly passes generate_ai_summary=False,
    # so no Gemini call should happen on this path -- but generate_summary is
    # mocked anyway as a defensive belt-and-suspenders measure, since these
    # tests are only about the rate limit and length cap, not AI-summary
    # generation, and a future change to that route shouldn't turn these
    # tests into ones that make real Gemini calls.
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="a summary"))


def test_api_resolve_rejects_after_limit_with_no_further_simbad_calls(monkeypatch):
    call_counter = {"count": 0}
    _mock_simbad(monkeypatch, call_counter)
    monkeypatch.setattr(main_mod, "RESOLVE_RATE_LIMIT", 3)

    client = TestClient(app)

    for i in range(3):
        response = client.get(f"/api/resolve?q=star-{i}")
        assert response.status_code == 200

    assert call_counter["count"] == 3

    response = client.get("/api/resolve?q=star-4")
    assert response.status_code == 429
    # No further SIMBAD call should have been made once the limit is hit.
    assert call_counter["count"] == 3


def test_api_resolve_rejects_query_longer_than_max_length(monkeypatch):
    call_counter = {"count": 0}
    _mock_simbad(monkeypatch, call_counter)

    client = TestClient(app)
    long_q = "x" * 201

    response = client.get(f"/api/resolve?q={long_q}")

    assert response.status_code == 422
    assert call_counter["count"] == 0


def test_search_allows_requests_under_the_limit(monkeypatch):
    call_counter = {"count": 0}
    _mock_simbad(monkeypatch, call_counter)
    monkeypatch.setattr(main_mod, "RESOLVE_RATE_LIMIT", 5)

    client = TestClient(app)

    for i in range(5):
        response = client.get(f"/search?q=star-{i}")
        assert response.status_code == 200

    assert call_counter["count"] == 5


def test_object_profile_rejects_after_limit_with_no_further_simbad_calls(monkeypatch):
    """TICKET-R3-01 / Finding F1: GET /object/{id} must be gated by the same
    resolve rate limiter as /search and /api/resolve. Each request below
    targets a distinct, never-before-cached id so every one is a genuine
    cache miss that would otherwise reach get_or_resolve() unthrottled."""
    call_counter = {"count": 0}
    _mock_simbad(monkeypatch, call_counter)
    monkeypatch.setattr(main_mod, "RESOLVE_RATE_LIMIT", 3)

    client = TestClient(app)

    for i in range(3):
        response = client.get(f"/object/distinct-object-{i}")
        assert response.status_code == 200

    assert call_counter["count"] == 3

    response = client.get("/object/distinct-object-4")
    assert response.status_code == 429
    # No further SIMBAD call should have been made once the limit is hit.
    assert call_counter["count"] == 3


def test_object_profile_cache_hit_is_never_rate_limited(monkeypatch):
    """A cache hit on /object/{id} must stay free, exactly as it does on
    /search and /api/resolve -- only the code path that triggers a live
    resolution (obj is None) should be gated."""
    call_counter = {"count": 0}
    _mock_simbad(monkeypatch, call_counter)
    monkeypatch.setattr(main_mod, "RESOLVE_RATE_LIMIT", 1)

    client = TestClient(app)

    # First request is a cache miss (the path segment is never a hit on the
    # first lookup), consuming the only allotted rate-limit slot. The mock
    # always resolves to main_id "* alf Ori" regardless of query text, so
    # subsequent lookups by that same main_id are genuine cache hits.
    first = client.get("/object/repeat-object")
    assert first.status_code == 200
    assert call_counter["count"] == 1

    # Subsequent requests keyed on the now-cached SIMBAD main_id must not be
    # rate limited or trigger further SIMBAD calls, even though the limit of
    # 1 has already been used up.
    for _ in range(5):
        response = client.get("/object/* alf Ori")
        assert response.status_code == 200

    assert call_counter["count"] == 1
