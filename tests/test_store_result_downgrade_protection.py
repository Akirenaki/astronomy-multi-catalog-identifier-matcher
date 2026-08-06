"""Regression tests for:

TICKET-01 / P0-1: an inline Gemini failure inside store_result() must not
propagate and crash the caller -- generate_ai_summary=True is now a
belt-and-suspenders safety net (the actual fix is that /api/resolve no longer
requests generate_ai_summary=True at all; see test_object_summary_route.py /
test_resolve_rate_limit.py-adjacent coverage for that route-level change).

TICKET-02 / P0-2: store_result() must not let a lower-quality re-resolution
(e.g. a transient SIMBAD outage on a routine TTL refresh) overwrite or delete
data from a higher-quality cached result.
"""

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from sqlalchemy import select

from app import cache as cache_mod
from app.database import SessionLocal, engine, init_db
from app.models import Base, IdentifierRecord, ObjectRecord, PlanetRecord
from app.narrative import GeminiGenerationError
from app.resolver import ResolutionResult


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def _resolved_result(query_text: str = "51 Peg") -> ResolutionResult:
    return ResolutionResult(
        query_text=query_text,
        state="RESOLVED",
        main_id="51 Peg",
        ra=344.36,
        dec=20.77,
        otype="Star",
        spectral_type="G2V",
        aliases=["51 Peg", "HD 217014"],
        resolved_via=[query_text, "51 Peg"],
    )


def _lookup_failed_result(query_text: str = "51 Peg") -> ResolutionResult:
    return ResolutionResult(query_text=query_text, state="LOOKUP_FAILED")


# ---------------------------------------------------------------------------
# TICKET-01
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_result_survives_gemini_failure_when_summary_requested(monkeypatch):
    """A Gemini failure during the inline (generate_ai_summary=True) summary
    call must not raise out of store_result(); the catalog data should still
    be persisted, just without an ai_summary."""

    async def _boom(*args, **kwargs):
        raise GeminiGenerationError("simulated Gemini outage")

    monkeypatch.setattr(cache_mod, "generate_summary", _boom)

    record = await cache_mod.store_result(_resolved_result(), generate_ai_summary=True)

    assert record.resolution_state == "RESOLVED"
    assert record.simbad_main_id == "51 Peg"
    assert record.ai_summary is None


@pytest.mark.asyncio
async def test_store_result_sets_generated_at_when_summary_requested(monkeypatch):
    """[F3] A successful inline (generate_ai_summary=True) summary call must
    set ai_summary_generated_at, same as ensure_ai_summary()/
    regenerate_ai_summary() do -- the README documents this as an invariant
    ("ai_summary_generated_at: Set on real (re)generation only, never on a
    cache hit"), and regenerate_ai_summary()'s cooldown check is gated on
    this field being non-None whenever ai_summary is set."""

    async def _fake_summary(*args, **kwargs):
        return "a generated summary"

    monkeypatch.setattr(cache_mod, "generate_summary", _fake_summary)

    before = datetime.now(timezone.utc)
    record = await cache_mod.store_result(_resolved_result(), generate_ai_summary=True)

    assert record.ai_summary == "a generated summary"
    assert record.ai_summary_generated_at is not None
    assert record.ai_summary_generated_at >= before - timedelta(seconds=5)


# ---------------------------------------------------------------------------
# TICKET-02
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failure_does_not_erase_resolved_object():
    """A RESOLVED row that later re-resolves as LOOKUP_FAILED (e.g. SIMBAD
    hiccup on TTL refresh) must keep its original identity data, aliases,
    and ai_summary."""
    first = await cache_mod.store_result(_resolved_result(), generate_ai_summary=False)
    async with SessionLocal() as session:
        row = await session.get(ObjectRecord, first.id)
        row.ai_summary = "A Sun-like star known for its hot Jupiter companion."
        row.ai_summary_generated_at = datetime.now(timezone.utc)
        await session.commit()

    second = await cache_mod.store_result(_lookup_failed_result(), generate_ai_summary=False)

    assert second.id == first.id
    assert second.resolution_state == "RESOLVED"
    assert second.simbad_main_id == "51 Peg"
    assert second.ra_deg == pytest.approx(344.36)
    assert second.dec_deg == pytest.approx(20.77)
    assert second.ai_summary is not None

    async with SessionLocal() as session:
        aliases = (
            (await session.execute(select(IdentifierRecord).where(IdentifierRecord.object_id == first.id)))
            .scalars()
            .all()
        )
    assert len(aliases) == 2


@pytest.mark.asyncio
async def test_transient_failure_still_refreshes_expiry_for_retry():
    """Even though the downgrade is rejected, expires_at should still move to
    the short failed-state TTL so the object is retried soon rather than
    waiting out the original 14-day RESOLVED TTL."""
    first = await cache_mod.store_result(_resolved_result(), generate_ai_summary=False)
    original_expiry = first.expires_at

    second = await cache_mod.store_result(_lookup_failed_result(), generate_ai_summary=False)

    assert second.expires_at < original_expiry
    assert second.expires_at <= datetime.now(timezone.utc) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_equal_or_better_resolution_still_overwrites():
    """Sanity check that the downgrade guard doesn't break the ordinary
    same-or-better-quality update path (e.g. a RESOLVED re-resolution with
    updated spectral type)."""
    first = await cache_mod.store_result(_resolved_result(), generate_ai_summary=False)

    updated = ResolutionResult(
        query_text="51 Peg",
        state="RESOLVED",
        main_id="51 Peg",
        ra=344.36,
        dec=20.77,
        otype="Star",
        spectral_type="G5V",  # changed
        aliases=["51 Peg", "HD 217014"],
        resolved_via=["51 Peg", "51 Peg"],
    )
    second = await cache_mod.store_result(updated, generate_ai_summary=False)

    assert second.id == first.id
    assert second.spectral_type == "G5V"


@pytest.mark.asyncio
async def test_downgrade_from_partial_to_lookup_failed_preserves_planets():
    """PARTIAL (planets_lookup_failed=True) outranks LOOKUP_FAILED, so a
    subsequent LOOKUP_FAILED re-resolution must not wipe out planets that
    were stored the last time planet lookup actually succeeded."""
    resolved_with_planets = _resolved_result()
    first = await cache_mod.store_result(resolved_with_planets, generate_ai_summary=False)
    async with SessionLocal() as session:
        session.add(
            PlanetRecord(
                object_id=first.id,
                pl_name="51 Peg b",
                pl_letter="b",
                orbital_period_days=4.23,
            )
        )
        await session.commit()

    second = await cache_mod.store_result(_lookup_failed_result(), generate_ai_summary=False)

    async with SessionLocal() as session:
        planets = (
            (await session.execute(select(PlanetRecord).where(PlanetRecord.object_id == second.id)))
            .scalars()
            .all()
        )
    assert [p.pl_name for p in planets] == ["51 Peg b"]


@pytest.mark.asyncio
async def test_confirmed_no_planets_survives_a_later_lookup_failure():
    """P1-1: a confirmed PARTIAL (planets_lookup_failed=False) result must not
    be silently overwritten by a same-state PARTIAL (planets_lookup_failed=True)
    result -- both share the same _STATE_QUALITY score, so the cross-state
    downgrade guard alone doesn't catch this transition."""
    confirmed = ResolutionResult(
        query_text="Barnard's Star",
        state="PARTIAL",
        main_id="Barnard's Star",
        ra=269.45,
        dec=4.69,
        otype="Star",
        spectral_type="M4V",
        aliases=["Barnard's Star", "HIP 87937"],
        resolved_via=["Barnard's Star", "Barnard's Star"],
        planets_lookup_failed=False,
    )
    first = await cache_mod.store_result(confirmed, generate_ai_summary=False)
    assert first.planets_lookup_failed is False

    later_failure = ResolutionResult(
        query_text="Barnard's Star",
        state="PARTIAL",
        main_id="Barnard's Star",
        ra=269.45,
        dec=4.69,
        otype="Star",
        spectral_type="M4V",
        aliases=["Barnard's Star", "HIP 87937"],
        resolved_via=["Barnard's Star", "Barnard's Star"],
        planets_lookup_failed=True,
    )
    second = await cache_mod.store_result(later_failure, generate_ai_summary=False)

    assert second.id == first.id
    assert second.resolution_state == "PARTIAL"
    assert second.planets_lookup_failed is False  # must NOT flip to True


@pytest.mark.asyncio
async def test_unconfirmed_partial_is_overwritten_by_confirmed_no_planets():
    """Mirror direction: an unconfirmed PARTIAL (lookup failed) followed by a
    confirmed PARTIAL (zero planets, successfully checked) is an improvement
    and must still overwrite normally -- guards against an overly aggressive
    fix that blocks same-state PARTIAL updates in both directions."""
    unconfirmed = ResolutionResult(
        query_text="Barnard's Star",
        state="PARTIAL",
        main_id="Barnard's Star",
        ra=269.45,
        dec=4.69,
        otype="Star",
        spectral_type="M4V",
        aliases=["Barnard's Star", "HIP 87937"],
        resolved_via=["Barnard's Star", "Barnard's Star"],
        planets_lookup_failed=True,
    )
    first = await cache_mod.store_result(unconfirmed, generate_ai_summary=False)
    assert first.planets_lookup_failed is True

    confirmed = ResolutionResult(
        query_text="Barnard's Star",
        state="PARTIAL",
        main_id="Barnard's Star",
        ra=269.45,
        dec=4.69,
        otype="Star",
        spectral_type="M4V",
        aliases=["Barnard's Star", "HIP 87937"],
        resolved_via=["Barnard's Star", "Barnard's Star"],
        planets_lookup_failed=False,
    )
    second = await cache_mod.store_result(confirmed, generate_ai_summary=False)

    assert second.id == first.id
    assert second.resolution_state == "PARTIAL"
    assert second.planets_lookup_failed is False  # improvement direction still overwrites


def _confirmed_no_planets_partial(query_text: str = "51 Peg") -> ResolutionResult:
    return ResolutionResult(
        query_text=query_text,
        state="PARTIAL",
        main_id="51 Peg",
        ra=344.36,
        dec=20.77,
        otype="Star",
        spectral_type="G2V",
        aliases=["51 Peg", "HD 217014"],
        planets=[],
        planets_lookup_failed=False,
        resolved_via=[query_text, "51 Peg"],
    )


@pytest.mark.asyncio
async def test_downgrade_to_confirmed_partial_still_refreshes_expiry_for_retry():
    """F1 (round 4): a RESOLVED->confirmed-PARTIAL downgrade (planets_lookup_failed=False)
    must still advance expires_at to the short retry TTL, same as every other downgrade
    path -- otherwise the row's cache entry never becomes fresh again and every future
    request re-triggers a live SIMBAD + Exoplanet Archive lookup forever."""
    first = await cache_mod.store_result(_resolved_result(), generate_ai_summary=False)
    async with SessionLocal() as session:
        row = await session.get(ObjectRecord, first.id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.commit()

    second = await cache_mod.store_result(_confirmed_no_planets_partial(), generate_ai_summary=False)

    assert second.resolution_state == "RESOLVED"
    assert second.expires_at > datetime.now(timezone.utc)
    assert second.expires_at <= datetime.now(timezone.utc) + timedelta(hours=1, minutes=1)

    cached = await cache_mod.get_cached("51 Peg")
    assert cached is not None


def _result_for_state(state: str, *, planets_lookup_failed: bool = False, query_text: str = "F1-02 Table Test") -> ResolutionResult:
    """Build a minimal ResolutionResult for a given state, for the table-driven
    expiry test below. Mirrors the fixtures used elsewhere in this file."""
    if state == "RESOLVED":
        return ResolutionResult(
            query_text=query_text, state="RESOLVED", main_id="F1-02 Star", ra=10.0, dec=20.0,
            otype="Star", spectral_type="G2V", aliases=["F1-02 Star"],
            planets=[{"pl_name": "F1-02 Star b"}], resolved_via=[query_text, "F1-02 Star"],
        )
    if state == "PARTIAL":
        return ResolutionResult(
            query_text=query_text, state="PARTIAL", main_id="F1-02 Star", ra=10.0, dec=20.0,
            otype="Star", spectral_type="G2V", aliases=["F1-02 Star"], planets=[],
            planets_lookup_failed=planets_lookup_failed, resolved_via=[query_text, "F1-02 Star"],
        )
    if state == "AMBIGUOUS":
        return ResolutionResult(
            query_text=query_text, state="AMBIGUOUS",
            candidates=[
                {"main_id": "F1-02 Star", "ra": 10.0, "dec": 20.0, "otype": "Star", "sp_type": "G2V", "aliases": []},
                {"main_id": "F1-02 Star B", "ra": 10.1, "dec": 20.1, "otype": "Star", "sp_type": "M4V", "aliases": []},
            ],
        )
    if state == "UNRESOLVED":
        return ResolutionResult(query_text=query_text, state="UNRESOLVED")
    if state == "LOOKUP_FAILED":
        return ResolutionResult(query_text=query_text, state="LOOKUP_FAILED")
    raise ValueError(state)


_ALL_STATE_CONFIGS = [
    ("RESOLVED", False),
    ("PARTIAL", False),
    ("PARTIAL", True),
    ("AMBIGUOUS", False),
    ("UNRESOLVED", False),
    ("LOOKUP_FAILED", False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_config", _ALL_STATE_CONFIGS)
@pytest.mark.parametrize("second_config", _ALL_STATE_CONFIGS)
async def test_expiry_is_always_refreshed_to_the_future_for_downgrades_or_flagged_states(
    first_config, second_config
):
    """F1-02 (round 4): table-driven regression covering every
    (first_state, second_state, planets_lookup_failed) combination.

    Whenever the second store_result() call represents a downgrade from the
    first (per _is_downgrade()), or the second result's own state/flag
    combination falls into the explicit short-TTL list, expires_at must end
    up refreshed to a future value. This closes the whole bug class F1
    belonged to, not just the single RESOLVED->confirmed-PARTIAL case F1-01's
    test covers.
    """
    first_state, first_failed = first_config
    second_state, second_failed = second_config
    query_text = f"F1-02 Table Test {first_state}-{first_failed}->{second_state}-{second_failed}"

    first = await cache_mod.store_result(
        _result_for_state(first_state, planets_lookup_failed=first_failed, query_text=query_text),
        generate_ai_summary=False,
    )
    async with SessionLocal() as session:
        row = await session.get(ObjectRecord, first.id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.commit()

    is_downgrade = cache_mod._is_downgrade(second_state, second_failed, first_state, first_failed)
    second = await cache_mod.store_result(
        _result_for_state(second_state, planets_lookup_failed=second_failed, query_text=query_text),
        generate_ai_summary=False,
    )

    short_ttl_state = second_state in ("UNRESOLVED", "AMBIGUOUS", "LOOKUP_FAILED") or (
        second_state == "PARTIAL" and second_failed
    )

    if is_downgrade or short_ttl_state:
        assert second.expires_at > datetime.now(timezone.utc), (
            f"{first_config} -> {second_config}: expires_at was not refreshed to the future"
        )
    else:
        # Genuine improvement (or equal, non-flagged state): the normal
        # 14-day fresh_fields expiry applies, which is also always in the
        # future relative to now.
        assert second.expires_at > datetime.now(timezone.utc)
