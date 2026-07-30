"""Regression test for F5: the stale "Cross-Matcher" branding (pre-dating the
repo's rename to astronomy-multi-catalog-identifier-matcher) must not appear
anywhere in the rendered homepage."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

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
