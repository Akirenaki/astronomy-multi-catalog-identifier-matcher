"""Regression test for F5: the stale "Cross-Matcher" branding (pre-dating the
repo's rename to astronomy-multi-catalog-identifier-matcher) must not appear
anywhere in the rendered homepage."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from unittest.mock import AsyncMock

import pytest_asyncio
from fastapi.testclient import TestClient

from app.database import engine, init_db
from app.main import app
from app.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def test_homepage_does_not_contain_stale_cross_matcher_branding():
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Cross-Matcher" not in response.text
    assert "Identifier-Matcher" in response.text
    assert "astronomy-multi-catalog-cross-matcher" not in response.text


def test_object_page_does_not_contain_stale_crossmatcher_localstorage_key(monkeypatch):
    """Regression test for Finding F4: the coordinate-viz panel's
    localStorage key must not still spell out the pre-rename project name."""

    async def fake_resolve_identity(*args, **kwargs):
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
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="a summary"))

    with TestClient(app) as client:
        response = client.get("/object/betelgeuse")

    assert response.status_code == 200
    assert "crossmatcher" not in response.text.lower()


def test_contact_and_legal_page_has_contact_information():
    with TestClient(app) as client:
        response = client.get("/legal")

    assert response.status_code == 200
    assert "narendramal4869@gmail.com" in response.text
    assert "https://github.com/Akirenaki" in response.text
    assert "https://www.linkedin.com/in/renee-astraea" in response.text
    assert "Contact" in response.text
