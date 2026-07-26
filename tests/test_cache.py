"""Tests for cache behavior and cached resolution lookups."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import os

import pytest
import pytest_asyncio

# Use an isolated on-disk test database so these tests never touch the app's
# real astronomy.db, and so the engine binds to this URL before app.database
# is imported anywhere else in the test session.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from app import cache as cache_mod
from app.database import engine, init_db
from app.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    """Give every test a genuinely empty database, not just tables that exist.

    init_db() only runs Base.metadata.create_all, which is a no-op once the tables
    already exist -- it does not clear rows. Because this suite uses a persistent
    on-disk file (astronomy_test_cache.db) rather than an in-memory database, running
    `pytest` a second time within the same TTL window would silently reuse rows left
    behind by the previous run. Concretely: test_ambiguous_candidates_survive_a_cache_hit
    inserts a row for query_text="Alpha Cache Test" with a 1-hour TTL; on a second
    `pytest` invocation shortly after, that row is still valid, so the test's *first*
    get_or_resolve() call hits the cache instead of the mocked resolver, and the
    `resolve_mock.await_count == 1` assertion fails with 0. Dropping and recreating
    the schema before every test function removes that dependency on how much time (or
    how many prior runs) have passed since the file was last touched.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


@pytest.mark.asyncio
async def test_ambiguous_result_persists_candidates(monkeypatch):
    """The core bug this test guards against: AMBIGUOUS results used to be
    stored with no candidate data at all, so the disambiguation list the
    spec requires was silently dropped before it ever reached the page."""

    fake_candidates = [
        {"main_id": "51 Peg", "ra": 344.36, "dec": 20.77, "otype": "Star", "sp_type": "G2V", "aliases": ["HD 217014"]},
        {"main_id": "51 Peg B", "ra": 344.37, "dec": 20.78, "otype": "Star", "sp_type": "M4V", "aliases": ["HD 217014 B"]},
    ]

    monkeypatch.setattr(
        "app.resolver.resolve_identity", AsyncMock(return_value=fake_candidates)
    )
    monkeypatch.setattr(
        "app.resolver.find_planets", AsyncMock(return_value=([], None, False))
    )

    record = await cache_mod.get_or_resolve("51 Peg Ambiguous Test")

    assert record.resolution_state == "AMBIGUOUS"
    assert len(record.candidates) == 2
    assert {c["main_id"] for c in record.candidates} == {"51 Peg", "51 Peg B"}

    # No confirmed object yet -> no AI summary should be generated/cached.
    assert record.ai_summary is None

    # Short, self-healing TTL (like UNRESOLVED), not the full 14-day TTL.
    remaining = record.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert remaining.total_seconds() < 2 * 60 * 60


@pytest.mark.asyncio
async def test_lookup_failed_gets_short_ttl_and_no_summary(monkeypatch):
    """A SIMBAD lookup that failed outright (timeout/transport error) must be cached
    the same self-healing way as UNRESOLVED/AMBIGUOUS: a short TTL so it's retried
    soon (e.g. once the network issue clears) rather than sitting stale for 14 days,
    and no AI summary, since there is no confirmed structured data to describe."""
    from app.catalogs.simbad import SimbadLookupError

    async def failing_simbad(query_text: str):
        raise SimbadLookupError("simulated network failure")

    monkeypatch.setattr("app.resolver.resolve_identity", failing_simbad)
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))

    record = await cache_mod.get_or_resolve("HD 217014 Lookup Failed Test")

    assert record.resolution_state == "LOOKUP_FAILED"
    assert record.ai_summary is None

    remaining = record.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert remaining.total_seconds() < 2 * 60 * 60


@pytest.mark.asyncio
async def test_ambiguous_candidates_survive_a_cache_hit(monkeypatch):
    """Re-searching within the TTL window should serve the cached candidate
    list rather than dropping it on the second lookup."""

    fake_candidates = [
        {"main_id": "Alpha A", "ra": 1.0, "dec": 2.0, "otype": "Star", "sp_type": "G2V", "aliases": []},
        {"main_id": "Alpha B", "ra": 1.1, "dec": 2.1, "otype": "Star", "sp_type": "K1V", "aliases": []},
    ]
    resolve_mock = AsyncMock(return_value=fake_candidates)
    monkeypatch.setattr("app.resolver.resolve_identity", resolve_mock)
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))

    first = await cache_mod.get_or_resolve("Alpha Cache Test")
    second = await cache_mod.get_or_resolve("Alpha Cache Test")

    assert first.candidates == second.candidates
    # Second call should be served from cache -> SIMBAD not queried again.
    assert resolve_mock.await_count == 1


@pytest.mark.asyncio
async def test_resolved_record_relationships_readable_after_session_closes(monkeypatch):
    """The core bug this test guards against: result.html and to_dict() both read
    `.identifiers` and `.planets` on the ObjectRecord returned by get_or_resolve(), well
    after the SessionLocal() context that produced it has closed. Those are lazy-loaded
    relationships -- touching them outside an open session used to raise
    sqlalchemy.orm.exc.DetachedInstanceError, which surfaced to real users as a 500
    "Internal Server Error" on essentially every search (RESOLVED, PARTIAL, and
    UNRESOLVED all read .identifiers/.planets in the template; only AMBIGUOUS skipped it).
    """
    monkeypatch.setattr(
        "app.resolver.resolve_identity",
        AsyncMock(
            return_value={
                "main_id": "51 Peg",
                "ra": 344.36,
                "dec": 20.77,
                "otype": "Star",
                "sp_type": "G2V",
                "aliases": ["HD 217014"],
            }
        ),
    )
    monkeypatch.setattr(
        "app.resolver.find_planets",
        AsyncMock(return_value=([{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014", False)),
    )

    record = await cache_mod.get_or_resolve("51 Peg Relationship Test")

    # No exception should be raised reaching into these relationships, even though the
    # session used inside get_or_resolve() has already been closed by this point.
    assert len(record.identifiers) == 1
    assert record.identifiers[0].identifier == "HD 217014"
    assert len(record.planets) == 1
    assert record.planets[0].pl_name == "51 Peg b"

    # Also confirm a cache hit (get_cached path, not store_result) survives the same way.
    cached_record = await cache_mod.get_or_resolve("51 Peg Relationship Test")
    assert len(cached_record.identifiers) == 1
    assert len(cached_record.planets) == 1


@pytest.mark.asyncio
async def test_get_or_resolve_generate_ai_summary_false_skips_gemini(monkeypatch):
    """Regression test for the data-first loading UX: get_or_resolve(...,
    generate_ai_summary=False) must render/store the object without calling Gemini at
    all, so /search can return as soon as SIMBAD + the Exoplanet Archive respond,
    instead of also waiting on the (sometimes ~42s) narrative call."""
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
    summary_mock = AsyncMock(return_value="should not be called")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    record = await cache_mod.get_or_resolve("Betelgeuse Deferred Summary Test", generate_ai_summary=False)

    assert record.resolution_state == "PARTIAL"
    assert record.ai_summary is None
    summary_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_ai_summary_generates_and_persists_once(monkeypatch):
    """ensure_ai_summary() is the endpoint the frontend polls after the main page has
    already rendered. It must: generate the summary on first call, persist it, and
    NOT call Gemini again on a second call for the same object (repeated page views /
    accidental double-fetches shouldn't burn quota re-generating an unchanged result)."""
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
    summary_mock = AsyncMock(return_value="Betelgeuse is a huge red star with no known planets.")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    record = await cache_mod.get_or_resolve("Ensure Summary Test", generate_ai_summary=False)
    assert record.ai_summary is None  # confirms the deferred-generation precondition

    first = await cache_mod.ensure_ai_summary(record.simbad_main_id)
    second = await cache_mod.ensure_ai_summary(record.simbad_main_id)

    assert first == "Betelgeuse is a huge red star with no known planets."
    assert second == first
    summary_mock.assert_awaited_once()

    persisted = await cache_mod.get_object_by_simbad_id(record.simbad_main_id)
    assert persisted.ai_summary == first


@pytest.mark.asyncio
async def test_ensure_ai_summary_skips_gemini_for_non_confirmed_states(monkeypatch):
    """Mirrors store_result()'s own skip rule: AMBIGUOUS/UNRESOLVED/LOOKUP_FAILED have
    no confirmed structured data, so ensure_ai_summary() must not call Gemini for such
    a row even if something requests it. In practice these states never get a
    simbad_main_id from the real resolve pipeline, so this test inserts the row
    directly to exercise that branch on its own, independent of the resolver."""
    from datetime import timedelta

    from app.database import SessionLocal
    from app.models import ObjectRecord

    async with SessionLocal() as session:
        session.add(
            ObjectRecord(
                query_text="Directly Inserted Unresolved Row",
                simbad_main_id="Synthetic Test Id",
                resolution_state="UNRESOLVED",
                resolved_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await session.commit()

    summary_mock = AsyncMock(return_value="should not be called")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    result = await cache_mod.ensure_ai_summary("Synthetic Test Id")

    assert result == "No summary available."
    summary_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_ai_summary_raises_lookup_error_for_unknown_id():
    """A request for a summary of an id that was never resolved (typo'd URL, stale
    link, etc.) must raise, not silently fabricate a response."""
    with pytest.raises(LookupError):
        await cache_mod.ensure_ai_summary("this-id-does-not-exist")


@pytest.mark.asyncio
async def test_alias_search_reuses_cached_object_and_preserves_ai_summary(monkeypatch):
    """Regression test for EVALUATION.md 1.4: a different query string for an object
    that's already cached (e.g. "51 Peg" vs "51 Pegasi", both resolving to the same
    SIMBAD main_id) must not trigger its own full SIMBAD + Exoplanet Archive round
    trip once that object has already been seen under any alias, and must never
    silently discard a previously generated AI summary along the way."""
    simbad_mock = AsyncMock(
        return_value={
            "main_id": "51 Peg",
            "ra": 344.36,
            "dec": 20.77,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }
    )
    monkeypatch.setattr("app.resolver.resolve_identity", simbad_mock)
    monkeypatch.setattr(
        "app.resolver.find_planets",
        AsyncMock(return_value=([{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014", False)),
    )
    summary_mock = AsyncMock(return_value="A Sun-like star with one known planet.")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    # First search: a genuine first-time resolution. Generates the summary.
    first = await cache_mod.get_or_resolve("51 Peg")
    assert first.ai_summary == "A Sun-like star with one known planet."
    assert simbad_mock.await_count == 1
    summary_mock.assert_awaited_once()

    # Second search, a *different*, never-before-seen alias for the same object:
    # this is still a genuine cache miss (this exact string has never been
    # resolved), so it does re-resolve once -- but store_result() must reuse the
    # existing row (matched via simbad_main_id) rather than deleting and
    # recreating it, which is what would have discarded the summary before this fix.
    second = await cache_mod.get_or_resolve("51 Pegasi")
    assert second.id == first.id
    assert second.ai_summary == "A Sun-like star with one known planet."
    assert simbad_mock.await_count == 2
    summary_mock.assert_awaited_once()  # still only the one generation, ever

    # Third search, back to the *original* query string: by now the row's
    # query_text has moved on to "51 Pegasi" (the most recently resolved alias),
    # so a direct ObjectRecord.query_text match would miss -- this must be served
    # via the QueryAlias fallback instead of resolving a third time.
    third = await cache_mod.get_or_resolve("51 Peg")
    assert third.id == first.id
    assert third.ai_summary == "A Sun-like star with one known planet."
    assert simbad_mock.await_count == 2  # unchanged: served from the alias index


@pytest.mark.asyncio
async def test_ai_summary_cleared_on_re_resolution_with_changed_planet_data(monkeypatch):
    """Regression test for F6/TICKET-04: if a cached row is re-resolved (e.g. after
    its TTL expires) and the new resolution has a different planet count than the
    old one, the existing (now stale) ai_summary must be cleared rather than left
    describing data that's since changed."""
    simbad_mock = AsyncMock(
        return_value={
            "main_id": "51 Peg",
            "ra": 344.36,
            "dec": 20.77,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }
    )
    monkeypatch.setattr("app.resolver.resolve_identity", simbad_mock)
    monkeypatch.setattr(
        "app.resolver.find_planets",
        AsyncMock(return_value=([], "HD 217014", False)),
    )
    monkeypatch.setattr(
        "app.cache.generate_summary", AsyncMock(return_value="A Sun-like star with no known planets.")
    )

    first = await cache_mod.get_or_resolve("51 Peg")
    assert first.ai_summary == "A Sun-like star with no known planets."

    # Force the row to look expired so the next get_or_resolve() genuinely
    # re-resolves rather than serving the cache hit.
    async with cache_mod.SessionLocal() as session:
        row = await session.get(cache_mod.ObjectRecord, first.id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.commit()

    # Re-resolve with a newly-discovered planet.
    monkeypatch.setattr(
        "app.resolver.find_planets",
        AsyncMock(return_value=([{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014", False)),
    )

    second = await cache_mod.get_or_resolve("51 Peg", generate_ai_summary=False)
    assert second.id == first.id
    assert second.ai_summary is None
    assert second.ai_summary_generated_at is None


@pytest.mark.asyncio
async def test_ai_summary_preserved_on_re_resolution_with_unchanged_planet_data(monkeypatch):
    """Companion to the test above: re-resolving with an *unchanged* planet set
    and state must leave the existing ai_summary untouched."""
    simbad_mock = AsyncMock(
        return_value={
            "main_id": "51 Peg",
            "ra": 344.36,
            "dec": 20.77,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }
    )
    monkeypatch.setattr("app.resolver.resolve_identity", simbad_mock)
    monkeypatch.setattr(
        "app.resolver.find_planets",
        AsyncMock(return_value=([{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014", False)),
    )
    monkeypatch.setattr(
        "app.cache.generate_summary", AsyncMock(return_value="A Sun-like star with one known planet.")
    )

    first = await cache_mod.get_or_resolve("51 Peg")
    assert first.ai_summary == "A Sun-like star with one known planet."

    async with cache_mod.SessionLocal() as session:
        row = await session.get(cache_mod.ObjectRecord, first.id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.commit()

    # Re-resolve with the exact same planet (same pl_name) and state.
    second = await cache_mod.get_or_resolve("51 Peg", generate_ai_summary=False)
    assert second.id == first.id
    assert second.ai_summary == "A Sun-like star with one known planet."


@pytest.mark.asyncio
async def test_get_or_resolve_by_simbad_main_id_hits_cache(monkeypatch):
    """Regression test for EVALUATION.md 1.5: main.py's object_profile() falls back
    to get_or_resolve(simbad_main_id) whenever a direct get_object_by_simbad_id()
    lookup misses. Once an object has been resolved at all, looking it up again by
    its own canonical simbad_main_id (not the original search string) must be a
    cache hit, not a redundant live re-resolution."""
    simbad_mock = AsyncMock(
        return_value={
            "main_id": "* 51 Peg",
            "ra": 344.36,
            "dec": 20.77,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }
    )
    monkeypatch.setattr("app.resolver.resolve_identity", simbad_mock)
    monkeypatch.setattr("app.resolver.find_planets", AsyncMock(return_value=([], None, False)))

    first = await cache_mod.get_or_resolve("51 Peg")
    assert simbad_mock.await_count == 1

    # Looking the same object up by its own simbad_main_id (as object_profile()'s
    # fallback does) must not trigger a second SIMBAD round trip.
    by_main_id = await cache_mod.get_or_resolve(first.simbad_main_id)
    assert by_main_id.id == first.id
    assert simbad_mock.await_count == 1
