"""Regression tests for:

TICKET-07: get_cached() should treat differently-cased spellings of the same
query text as the same cache key.

TICKET-08: ResolutionResult.resolved_via must not contain empty-string
placeholders when matched_alias / main_id are absent.
"""

import os
from unittest.mock import AsyncMock

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio

from app import cache as cache_mod
from app.database import engine, init_db
from app.models import Base
from app.resolver import resolve_query


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


@pytest.mark.asyncio
async def test_get_or_resolve_is_case_insensitive_for_repeat_queries(monkeypatch):
    """Resolving 'Betelgeuse' and then 'betelgeuse' should hit SIMBAD once,
    not twice -- the second request should be served from cache."""

    fake_result = {
        "main_id": "* alf Ori",
        "ra": 88.79,
        "dec": 7.41,
        "otype": "Star",
        "sp_type": "M1-2Ia-Iab",
        "aliases": ["Betelgeuse"],
    }
    resolve_mock = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("app.resolver.resolve_identity", resolve_mock)
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))

    first = await cache_mod.get_or_resolve("Betelgeuse", generate_ai_summary=False)
    second = await cache_mod.get_or_resolve("betelgeuse", generate_ai_summary=False)

    assert resolve_mock.await_count == 1
    assert second.id == first.id


@pytest.mark.asyncio
async def test_get_cached_matches_stored_alias_case_insensitively(monkeypatch):
    """Same as above, but going through a stored QueryAlias row rather than
    the primary ObjectRecord.query_text, to cover both branches of
    get_cached()."""

    fake_result = {
        "main_id": "* alf Ori",
        "ra": 88.79,
        "dec": 7.41,
        "otype": "Star",
        "sp_type": "M1-2Ia-Iab",
        "aliases": ["Betelgeuse"],
    }
    resolve_mock = AsyncMock(return_value=fake_result)
    monkeypatch.setattr("app.resolver.resolve_identity", resolve_mock)
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))

    # First query establishes the primary row under "Betelgeuse".
    first = await cache_mod.get_or_resolve("Betelgeuse", generate_ai_summary=False)
    # A different query string that resolves to the same main_id becomes a
    # QueryAlias row (not the primary ObjectRecord.query_text).
    await cache_mod.get_or_resolve("alf Ori", generate_ai_summary=False)
    assert resolve_mock.await_count == 2

    # Now re-querying that alias in a different case should be a cache hit
    # against the QueryAlias row, not a third SIMBAD call.
    third = await cache_mod.get_or_resolve("ALF ORI", generate_ai_summary=False)

    assert resolve_mock.await_count == 2
    assert third.id == first.id


@pytest.mark.asyncio
async def test_resolved_via_has_no_empty_string_entries(monkeypatch):
    """A resolution where the query text itself is the canonical main_id (so
    there's no separate matched_alias) must not leave a blank '' entry in
    resolved_via."""

    fake_result = {
        "main_id": "51 Peg",
        "ra": 344.36,
        "dec": 20.77,
        "otype": "Star",
        "sp_type": "G2V",
        "aliases": [],
    }
    monkeypatch.setattr("app.resolver.resolve_identity", AsyncMock(return_value=fake_result))
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))

    result = await resolve_query("51 Peg")

    assert "" not in result.resolved_via
    assert all(step for step in result.resolved_via)
