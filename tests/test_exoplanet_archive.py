"""Unit tests for find_planets() against a *realistic* mocked HTTP response shape.

The regression this guards against: the NASA Exoplanet Archive's TAP `format=json`
response is a bare top-level JSON array of row objects, e.g.:

    [{"pl_name": "51 Peg b", "pl_letter": "b", ...}, ...]

NOT a dict wrapped in a `{"data": [...]}` envelope. An earlier version of this module
called `response.json().get("data", [])`, which raised AttributeError on every real
response (lists have no .get) -- silently swallowed by the broad except block, so
find_planets() always returned no planets no matter what the API actually said.

test_resolver.py deliberately does NOT catch this class of bug, because it monkeypatches
find_planets() itself rather than exercising the JSON-parsing code. These tests mock
httpx one layer lower, at the response object, so the real parsing logic runs.

This file also guards a second regression: find_planets() used to send one HTTP
request per alias, in sequence. For a star with many SIMBAD aliases (e.g. Betelgeuse
or Proxima Centauri, both 100+ cross-catalog identifiers), that meant potentially
100+ sequential round trips before concluding "no planets", which was slow enough to
time out an interactive search even though the app itself was still working. The
tests below confirm aliases are now sent as a single batched `IN (...)` query per
chunk instead.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.catalogs.exoplanet_archive import find_planets


def _fake_client(json_body):
    """Build a mock httpx.AsyncClient whose .post() returns a response with the given
    already-decoded JSON body, mirroring the real TAP endpoint's shape."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=json_body)

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post = AsyncMock(return_value=response)
    return client


@pytest.mark.asyncio
async def test_find_planets_parses_realistic_bare_array_response():
    """The core regression: a bare top-level array (the real API shape) must be
    parsed successfully, not silently treated as zero rows."""
    payload = [
        {
            "pl_name": "51 Peg b",
            "pl_letter": "b",
            "pl_orbper": 4.230785,
            "pl_rade": 1.9,
            "disc_year": 1995,
            "discoverymethod": "Radial Velocity",
            "hostname": "HD 217014",
        }
    ]
    with patch("httpx.AsyncClient", return_value=_fake_client(payload)):
        planets, matched_alias, lookup_failed = await find_planets(["HD 217014"])

    assert matched_alias == "HD 217014"
    assert len(planets) == 1
    assert planets[0]["pl_name"] == "51 Peg b"
    assert planets[0]["orbital_period_days"] == 4.230785
    assert planets[0]["discovery_year"] == 1995
    assert lookup_failed is False


@pytest.mark.asyncio
async def test_find_planets_returns_empty_for_genuinely_empty_array():
    """A star with no catalogued planets returns a real empty array from the API --
    confirm that still cleanly produces ([], None, False): a genuine, confirmed
    negative, not a failed lookup (see test_find_planets_reports_lookup_failed_when_a_chunk_errors_out
    below for the failure case this must be distinguishable from)."""
    with patch("httpx.AsyncClient", return_value=_fake_client([])):
        planets, matched_alias, lookup_failed = await find_planets(["Barnard's Star"])

    assert planets == []
    assert matched_alias is None
    assert lookup_failed is False


@pytest.mark.asyncio
async def test_find_planets_picks_first_alias_with_matching_rows_in_one_batch():
    """Regression test for the batching rewrite: aliases are now sent as a single
    `hostname IN (...)` query rather than one sequential request per alias. This test
    confirms that when the batched response contains rows for a *later* alias in the
    list (and nothing for an earlier one), the earlier alias's priority ordering is
    still respected when picking `matched_alias` -- and that only ONE request was made
    to cover both aliases, not two."""
    # Only "Alias Two" has matching rows in the archive; "Alias One" has none. Both are
    # returned (or not) from the same single batched response.
    payload = [{"pl_name": "Test b", "pl_letter": "b", "hostname": "Alias Two"}]

    client = _fake_client(payload)
    with patch("httpx.AsyncClient", return_value=client) as client_ctor:
        planets, matched_alias, lookup_failed = await find_planets(["Alias One", "Alias Two"])

    assert matched_alias == "Alias Two"
    assert planets[0]["pl_name"] == "Test b"
    assert lookup_failed is False
    # The core regression: exactly one HTTP request for both aliases, not one per alias.
    assert client_ctor.call_count == 1
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_find_planets_query_contains_every_alias_in_one_in_clause():
    """The batched query string itself should reference every alias passed in, so
    a star with many aliases still gets checked in a single round trip."""
    client = _fake_client([])
    with patch("httpx.AsyncClient", return_value=client):
        await find_planets(["HD 217014", "HIP 113357", "51 Peg"])

    sent_query = client.post.await_args.kwargs["data"]["query"]
    for alias in ("HD 217014", "HIP 113357", "51 Peg"):
        assert alias in sent_query


@pytest.mark.asyncio
async def test_find_planets_chunks_very_large_alias_lists():
    """A star with more aliases than fit in one batch (e.g. Betelgeuse or Proxima
    Centauri, which each carry 100+ SIMBAD cross-identifications) must still be fully
    checked -- split across multiple batched requests rather than truncated or sent as
    one arbitrarily long query string."""
    from app.catalogs import exoplanet_archive as module

    many_aliases = [f"Alias {i}" for i in range(module._BATCH_SIZE + 5)]
    client = _fake_client([])

    with patch("httpx.AsyncClient", return_value=client) as client_ctor:
        planets, matched_alias, lookup_failed = await find_planets(many_aliases)

    assert planets == []
    assert matched_alias is None
    assert lookup_failed is False
    # _BATCH_SIZE + 5 aliases, batches of _BATCH_SIZE, should be exactly 2 requests.
    assert client_ctor.call_count == 2


@pytest.mark.asyncio
async def test_find_planets_reports_lookup_failed_when_a_chunk_errors_out():
    """Regression test for EVALUATION.md 1.3: when a chunk's request fails outright
    (here, the mocked client raises), the resulting ([], None) must be flagged as
    lookup_failed=True rather than indistinguishable from a genuine "no planets"
    response, so callers don't cache it as a confident negative."""
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post = AsyncMock(side_effect=Exception("connection reset"))

    with patch("httpx.AsyncClient", return_value=client):
        planets, matched_alias, lookup_failed = await find_planets(["HD 217014"])

    assert planets == []
    assert matched_alias is None
    assert lookup_failed is True


@pytest.mark.asyncio
async def test_find_planets_lookup_failed_is_false_when_a_later_chunk_finds_a_match():
    """If an earlier chunk fails but a later chunk still finds a genuine match, that's
    a confirmed positive -- lookup_failed must be False, since the "no planets"
    caveat doesn't apply when planets *were* found."""
    from app.catalogs import exoplanet_archive as module

    failing_client = AsyncMock()
    failing_client.__aenter__.return_value = failing_client
    failing_client.__aexit__.return_value = False
    failing_client.post = AsyncMock(side_effect=Exception("timeout"))

    succeeding_payload = [{"pl_name": "Test b", "pl_letter": "b", "hostname": "Second Chunk Alias 0"}]
    succeeding_client = _fake_client(succeeding_payload)

    clients = iter([failing_client, succeeding_client])
    many_aliases = [f"Alias {i}" for i in range(module._BATCH_SIZE)] + ["Second Chunk Alias 0"]

    with patch("httpx.AsyncClient", side_effect=lambda *a, **k: next(clients)):
        planets, matched_alias, lookup_failed = await find_planets(many_aliases)

    assert matched_alias == "Second Chunk Alias 0"
    assert len(planets) == 1
    assert lookup_failed is False


@pytest.mark.asyncio
async def test_find_planets_skips_structurally_hostile_alias():
    """P2-4: an alias containing disallowed characters must not be sent upstream
    (not present in the outgoing IN (...) clause), and must not fail the whole batch."""
    client = _fake_client([])
    with patch("httpx.AsyncClient", return_value=client):
        planets, matched_alias, lookup_failed = await find_planets(
            ["51 Peg", "Alias'); DROP TABLE pscomppars; --"]
        )

    assert planets == []
    assert matched_alias is None
    assert lookup_failed is False  # skipped, not a lookup failure

    sent_query = client.post.await_args.kwargs["data"]["query"]
    assert "51 Peg" in sent_query
    assert "DROP TABLE" not in sent_query


@pytest.mark.asyncio
async def test_find_planets_allows_legitimate_aliases_with_special_chars():
    """The allow-list must not reject real designations already exercised elsewhere
    in the app (e.g. TYC/GJ-style identifiers with hyphens)."""
    client = _fake_client([])
    with patch("httpx.AsyncClient", return_value=client):
        await find_planets(["GJ 667 C", "TYC 1949-2020-1", "Barnard's Star"])

    sent_query = client.post.await_args.kwargs["data"]["query"]
    assert "GJ 667 C" in sent_query
    assert "TYC 1949-2020-1" in sent_query
