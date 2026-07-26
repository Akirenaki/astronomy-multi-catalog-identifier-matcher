"""Template-rendering test for F12/TICKET-06: a disambiguation candidate with
no resolvable main_id must not render a broken `/search?q=None` link."""

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


def test_candidate_with_no_main_id_does_not_render_q_none_link(monkeypatch):
    """A candidate row lacking main_id must render without a href, not
    `/search?q=None`."""
    fake_candidates = [
        {"main_id": "51 Peg", "ra": 344.36, "dec": 20.77, "otype": "Star", "sp_type": "G2V", "aliases": []},
        {"main_id": None, "ra": None, "dec": None, "otype": None, "sp_type": None, "aliases": []},
    ]
    monkeypatch.setattr("app.resolver.resolve_identity", AsyncMock(return_value=fake_candidates))
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))

    with TestClient(app) as client:
        response = client.get("/search?q=51+Peg+Ambiguous")

    assert response.status_code == 200
    assert "AMBIGUOUS" in response.text
    assert "q=None" not in response.text
    # The resolvable candidate's link should still be present and correct.
    assert "/search?q=51%20Peg" in response.text or "/search?q=51+Peg" in response.text
