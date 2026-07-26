"""Tests for F4/TICKET-03: the initial AI-summary-generation route must not be
a state-changing, unprotected GET. It should behave like the regenerate route:
POST + CSRF, and reject a missing/invalid token before calling Gemini."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest_asyncio
from fastapi.testclient import TestClient

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


def test_get_on_summary_route_is_not_allowed():
    """GET must be rejected outright (405), since the route is now POST-only."""
    with TestClient(app) as client:
        response = client.get(f"/object/{quote('* alf Ori', safe='')}/summary")

    assert response.status_code == 405


def test_post_without_csrf_token_is_rejected_before_gemini_is_called(monkeypatch):
    """A POST with no (or an invalid) X-CSRF-Token header must be rejected before
    Gemini is called and before any ai_summary is written."""
    _mock_betelgeuse(monkeypatch)
    summary_mock = AsyncMock(return_value="should not be called")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        client.get(f"/search?q=Betelgeuse")

        no_token = client.post(f"/object/{encoded_id}/summary")
        assert no_token.status_code == 403

        bad_token = client.post(
            f"/object/{encoded_id}/summary", headers={"X-CSRF-Token": "not-a-real-token"}
        )
        assert bad_token.status_code == 403

    summary_mock.assert_not_awaited()


def test_post_with_valid_csrf_token_behaves_like_the_old_get(monkeypatch):
    """With a valid token, the route should generate and return the summary
    exactly as the previous GET-based route did."""
    _mock_betelgeuse(monkeypatch)
    monkeypatch.setattr(
        "app.cache.generate_summary", AsyncMock(return_value="A red supergiant.")
    )
    encoded_id = quote("* alf Ori", safe="")

    with TestClient(app) as client:
        client.get("/search?q=Betelgeuse")
        token = get_csrf_token(client)
        response = client.post(
            f"/object/{encoded_id}/summary", headers={"X-CSRF-Token": token}
        )

    assert response.status_code == 200
    assert response.json()["summary"] == "A red supergiant."
