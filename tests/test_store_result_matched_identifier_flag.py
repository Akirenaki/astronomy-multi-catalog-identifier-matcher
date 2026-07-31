"""Regression test for TICKET-101 / Finding F1.

`resolver.py` may match the NASA Exoplanet Archive via a SIMBAD
type-classifier-stripped variant of an identifier (e.g. matching via
"51 Peg" for a stored alias of "* 51 Peg") that is never itself persisted
as an IdentifierRecord. Before this fix, `store_result()` flagged
`matched_exoplanet_archive` with a plain `alias == matched_alias`
comparison, which was always False in that scenario -- even though the
object correctly resolved with known planets. See
`tests/test_resolver.py::test_resolver_strips_simbad_type_prefix_for_exoplanet_cross_match`
for the resolver-layer version of this same scenario; this test extends
the guarantee to the persistence layer.
"""

import os

import pytest
import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from app.cache import store_result
from app.database import engine, init_db
from app.models import Base
from app.resolver import ResolutionResult


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


@pytest.mark.asyncio
async def test_store_result_flags_matched_identifier_via_stripped_type_prefix():
    """51 Peg: matched_alias ("51 Peg") is a stripped form not present in
    aliases (["HD 217014", "HIP 113357"]). At least one persisted
    IdentifierRecord for this RESOLVED, planet-bearing object must be
    flagged as matched -- it must never be uniformly False."""
    result = ResolutionResult(
        query_text="HD217014",
        state="RESOLVED",
        main_id="* 51 Peg",
        ra=344.36,
        dec=20.77,
        otype="Star",
        spectral_type="G2IV",
        aliases=["HD 217014", "HIP 113357"],
        planets=[{"pl_name": "51 Peg b", "pl_letter": "b"}],
        matched_alias="51 Peg",
        resolved_via=["HD217014", "* 51 Peg", "51 Peg"],
    )

    record = await store_result(result, generate_ai_summary=False)

    assert record.identifiers, "expected persisted IdentifierRecord rows"
    assert all(identifier.matched_exoplanet_archive for identifier in record.identifiers), (
        "matched_exoplanet_archive should not be uniformly False for a RESOLVED "
        "object whose Exoplanet Archive match came through main_id's stripped-prefix form "
        "-- since no single stored alias represents that match, every identifier of "
        "the same confirmed-planet-bearing object should be flagged"
    )


@pytest.mark.asyncio
async def test_store_result_matched_flag_still_works_for_unprefixed_alias():
    """Sanity check: the original, un-prefixed exact-match case still works."""
    result = ResolutionResult(
        query_text="Tau Ceti",
        state="RESOLVED",
        main_id="Tau Cet",
        ra=26.02,
        dec=-15.94,
        otype="Star",
        spectral_type="G8V",
        aliases=["HD 10700", "Tau Cet"],
        planets=[{"pl_name": "Tau Cet e", "pl_letter": "e"}],
        matched_alias="Tau Cet",
        resolved_via=["Tau Ceti", "Tau Cet"],
    )

    record = await store_result(result, generate_ai_summary=False)

    matched = {identifier.identifier for identifier in record.identifiers if identifier.matched_exoplanet_archive}
    assert matched == {"Tau Cet"}
