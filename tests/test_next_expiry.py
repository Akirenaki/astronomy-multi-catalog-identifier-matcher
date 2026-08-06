"""Pure, database-independent table-driven tests for app.cache._next_expiry().

Extracted per the round-4 Architecture Review as the "highest-value
architectural improvement": a standalone helper, unit-testable without a
database session, covering the full (is_downgrade, state,
planets_lookup_failed) combination space that F1 fell through a gap in.
"""

import pytest

from app import cache as cache_mod

_ALL_STATES = ["RESOLVED", "PARTIAL", "AMBIGUOUS", "UNRESOLVED", "LOOKUP_FAILED"]


def _expected_ttl(is_downgrade: bool, state: str, planets_lookup_failed: bool):
    if is_downgrade:
        return cache_mod._SHORT_CACHE_TTL
    if state in ("UNRESOLVED", "AMBIGUOUS", "LOOKUP_FAILED"):
        return cache_mod._SHORT_CACHE_TTL
    if state == "PARTIAL" and planets_lookup_failed:
        return cache_mod._SHORT_CACHE_TTL
    return cache_mod._FRESH_CACHE_TTL


@pytest.mark.parametrize("is_downgrade", [True, False])
@pytest.mark.parametrize("state", _ALL_STATES)
@pytest.mark.parametrize("planets_lookup_failed", [True, False])
def test_next_expiry_matches_expected_ttl_for_every_combination(is_downgrade, state, planets_lookup_failed):
    result = cache_mod._next_expiry(is_downgrade, state, planets_lookup_failed)
    assert result == _expected_ttl(is_downgrade, state, planets_lookup_failed)


def test_downgrade_always_short_regardless_of_new_state():
    """This is the exact case F1 missed: a downgrade to a state/flag
    combination that isn't itself in the explicit short-TTL list (e.g.
    RESOLVED -> a confirmed, non-failed PARTIAL) must still get the short
    TTL, because is_downgrade=True takes priority."""
    result = cache_mod._next_expiry(True, "PARTIAL", False)
    assert result == cache_mod._SHORT_CACHE_TTL


def test_non_downgrade_resolved_gets_fresh_ttl():
    result = cache_mod._next_expiry(False, "RESOLVED", False)
    assert result == cache_mod._FRESH_CACHE_TTL


def test_non_downgrade_confirmed_partial_gets_fresh_ttl():
    """A confirmed 'no planets' PARTIAL (planets_lookup_failed=False) that
    isn't a downgrade -- e.g. it's a fresh, first-time result -- should get
    the normal fresh TTL, not the short retry TTL reserved for unconfirmed
    PARTIAL/AMBIGUOUS/UNRESOLVED/LOOKUP_FAILED results."""
    result = cache_mod._next_expiry(False, "PARTIAL", False)
    assert result == cache_mod._FRESH_CACHE_TTL


def test_non_downgrade_unconfirmed_partial_gets_short_ttl():
    result = cache_mod._next_expiry(False, "PARTIAL", True)
    assert result == cache_mod._SHORT_CACHE_TTL
