"""Tests for the high-level astronomical object resolver."""

import pytest

from app.catalogs.simbad import SimbadLookupError
from app.resolver import resolve_query


@pytest.mark.asyncio
async def test_resolver_returns_unresolved_when_simbad_has_no_match(monkeypatch):
    """Verify that a missing SIMBAD match produces an UNRESOLVED state."""
    async def fake_simbad(query_text: str):
        return None

    async def fake_planets(alias_list):
        return [], None, False

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("not a star")

    assert result.state == "UNRESOLVED"
    assert result.planets == []
    assert result.aliases == []


@pytest.mark.asyncio
async def test_resolver_normalizes_whitespace_only_query_instead_of_falling_back_to_raw_text(monkeypatch):
    """TICKET-R3-02 / Finding F3: resolve_query() must use the normalized
    (empty) form of a whitespace-only query, not fall back to the raw,
    un-normalized input text."""

    async def fake_simbad(query_text: str):
        assert query_text == ""
        return None

    async def fake_planets(alias_list):
        return [], None, False

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("   ")

    assert result.query_text == ""
    assert result.state == "UNRESOLVED"


@pytest.mark.asyncio
async def test_resolver_returns_lookup_failed_when_simbad_is_unreachable(monkeypatch):
    """Core regression test for the network-vs-no-match distinction: when SIMBAD itself
    can't be reached (timeout, transport error, etc.), resolve_query() must report
    LOOKUP_FAILED, not UNRESOLVED -- the latter would falsely claim SIMBAD was checked
    and has no record of the object."""

    async def fake_simbad(query_text: str):
        raise SimbadLookupError("simulated network failure")

    async def fake_planets(alias_list):
        raise AssertionError("find_planets should never be called when SIMBAD couldn't be reached")

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("HD 217014")

    assert result.state == "LOOKUP_FAILED"
    assert result.planets == []


@pytest.mark.asyncio
async def test_resolver_returns_partial_when_simbad_matches_but_no_planets(monkeypatch):
    """Confirm that a SIMBAD hit without exoplanet data is reported as PARTIAL."""
    async def fake_simbad(query_text: str):
        return {
            "main_id": "51 Peg",
            "ra": 10.0,
            "dec": 20.0,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }

    async def fake_planets(alias_list):
        return [], None, False

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "PARTIAL"
    assert result.main_id == "51 Peg"
    assert result.planets == []


@pytest.mark.asyncio
async def test_resolver_returns_resolved_when_planet_match_found(monkeypatch):
    """Check that a SIMBAD hit with planet data becomes RESOLVED."""
    async def fake_simbad(query_text: str):
        return {
            "main_id": "51 Peg",
            "ra": 10.0,
            "dec": 20.0,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }

    async def fake_planets(alias_list):
        return [{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014", False

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "RESOLVED"
    assert result.planets[0]["pl_name"] == "51 Peg b"
    assert result.matched_alias == "HD 217014"


@pytest.mark.asyncio
async def test_resolver_returns_ambiguous_for_multiple_simbad_candidates(monkeypatch):
    """Ensure multiple SIMBAD candidates are surfaced as an AMBIGUOUS result."""
    async def fake_simbad(query_text: str):
        return [
            {
                "main_id": "51 Peg",
                "ra": 10.0,
                "dec": 20.0,
                "otype": "Star",
                "sp_type": "G2V",
                "aliases": ["HD 217014"],
            },
            {
                "main_id": "51 Peg B",
                "ra": 11.0,
                "dec": 21.0,
                "otype": "Star",
                "sp_type": "M4V",
                "aliases": ["HD 217014 B"],
            },
        ]

    async def fake_planets(alias_list):
        return [], None, False

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "AMBIGUOUS"
    assert len(result.candidates) == 2


@pytest.mark.asyncio
async def test_resolver_strips_simbad_type_prefix_for_exoplanet_cross_match(monkeypatch):
    """Regression test for a live-testing finding: SIMBAD's main_id/aliases carry a
    type-classifier prefix (e.g. "* 51 Peg") that the NASA Exoplanet Archive's plain
    hostname field ("51 Peg") does not use. An exact-string match against only the
    SIMBAD-formatted identifiers misses a textbook case like 51 Peg / 51 Peg b, which
    resolved as PARTIAL instead of RESOLVED against the real APIs before this fix."""

    async def fake_simbad(query_text: str):
        return {
            "main_id": "* 51 Peg",
            "ra": 344.36,
            "dec": 20.77,
            "otype": "Star",
            "sp_type": "G2IV",
            "aliases": ["HD 217014", "HIP 113357"],
        }

    seen_hostnames: list[str] = []

    async def fake_planets(alias_list):
        seen_hostnames.extend(alias_list)
        if "51 Peg" in alias_list:
            return [{"pl_name": "51 Peg b", "pl_letter": "b"}], "51 Peg", False
        return [], None, False

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("HD217014")

    assert result.state == "RESOLVED"
    assert result.matched_alias == "51 Peg"
    # The stripped form should be tried right after its prefixed original, not just
    # appended at the very end behind every other alias.
    assert seen_hostnames.index("51 Peg") < seen_hostnames.index("HIP 113357")


@pytest.mark.asyncio
async def test_resolver_flags_planets_lookup_failed_on_partial_result(monkeypatch):
    """Regression test for EVALUATION.md 1.3: when find_planets() reports it
    couldn't confirm "no planets" (its third return value), that must survive
    onto the ResolutionResult as planets_lookup_failed=True, still as state
    PARTIAL -- it's an unconfirmed negative, not its own resolution state."""
    async def fake_simbad(query_text: str):
        return {
            "main_id": "51 Peg",
            "ra": 10.0,
            "dec": 20.0,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }

    async def fake_planets(alias_list):
        return [], None, True

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "PARTIAL"
    assert result.planets == []
    assert result.planets_lookup_failed is True


@pytest.mark.asyncio
async def test_resolver_does_not_flag_planets_lookup_failed_when_resolved(monkeypatch):
    """A confirmed planet match must never carry planets_lookup_failed=True, even in
    principle -- the field only means anything for a PARTIAL "no planets" result."""
    async def fake_simbad(query_text: str):
        return {
            "main_id": "51 Peg",
            "ra": 10.0,
            "dec": 20.0,
            "otype": "Star",
            "sp_type": "G2V",
            "aliases": ["HD 217014"],
        }

    async def fake_planets(alias_list):
        return [{"pl_name": "51 Peg b", "pl_letter": "b"}], "HD 217014", False

    monkeypatch.setattr("app.resolver.resolve_identity", fake_simbad)
    monkeypatch.setattr("app.resolver.find_planets", fake_planets)

    result = await resolve_query("51 Peg")

    assert result.state == "RESOLVED"
    assert result.planets_lookup_failed is False
