"""Unit tests for resolve_identity() against a *realistic* mocked HTTP response shape.

The regression this guards against: SIMBAD's TAP `format=json` response follows the
standard IVOA TAP JSON envelope --

    {"metadata": [{"name": "main_id", ...}, ...], "data": [["* 51 Peg", 344.36, ...], ...]}

-- where each row in "data" is a POSITIONAL array, not a dict keyed by column name.
An earlier version of this module assumed every row already arrived as a dict (the
shape the *Exoplanet Archive* returns, not SIMBAD) and filtered on `isinstance(row, dict)`,
which silently dropped every real row and made every query resolve as UNRESOLVED.

test_resolver.py deliberately does NOT catch this class of bug, because it monkeypatches
resolve_identity() itself rather than exercising the JSON-parsing code. These tests mock
httpx one layer lower, at the response object, so the real parsing logic runs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.catalogs.simbad import SimbadLookupError, resolve_identity


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


def _tap_envelope(rows: list[list]) -> dict:
    """Build a realistic SIMBAD TAP JSON envelope for the columns this app queries."""
    return {
        "metadata": [
            {"name": "main_id", "datatype": "char"},
            {"name": "ra", "datatype": "double"},
            {"name": "dec", "datatype": "double"},
            {"name": "otype", "datatype": "char"},
            {"name": "sp_type", "datatype": "char"},
            {"name": "ids", "datatype": "char"},
        ],
        "data": rows,
    }


@pytest.mark.asyncio
async def test_resolve_identity_parses_realistic_metadata_data_envelope():
    """The core regression: a positional metadata+data envelope (the real API shape)
    must be parsed successfully into a single candidate dict, not treated as zero rows."""
    payload = _tap_envelope(
        [["51 Peg", 344.36, 20.77, "Star", "G2V", "HD 217014|HIP 113357|51 Peg"]]
    )
    with patch("httpx.AsyncClient", return_value=_fake_client(payload)):
        result = await resolve_identity("51 Peg")

    assert result is not None
    assert isinstance(result, dict)
    assert result["main_id"] == "51 Peg"
    assert result["sp_type"] == "G2V"
    assert "HD 217014" in result["aliases"]


@pytest.mark.asyncio
async def test_resolve_identity_returns_multiple_candidates_as_ambiguous_list():
    """Two rows in the envelope should come back as a list of candidates, not be
    silently collapsed into one and not be dropped for failing an isinstance(dict) check."""
    payload = _tap_envelope(
        [
            ["51 Peg", 344.36, 20.77, "Star", "G2V", "HD 217014"],
            ["51 Peg B", 344.37, 20.78, "Star", "M4V", "HD 217014 B"],
        ]
    )
    with patch("httpx.AsyncClient", return_value=_fake_client(payload)):
        result = await resolve_identity("51 Peg")

    assert isinstance(result, list)
    assert len(result) == 2
    assert {c["main_id"] for c in result} == {"51 Peg", "51 Peg B"}


@pytest.mark.asyncio
async def test_resolve_identity_returns_none_for_genuinely_empty_envelope():
    """A name SIMBAD has never heard of returns a real empty envelope -- confirm that
    still cleanly produces None, not an exception."""
    payload = _tap_envelope([])
    with patch("httpx.AsyncClient", return_value=_fake_client(payload)):
        result = await resolve_identity("asdkfjhasdf")

    assert result is None


@pytest.mark.asyncio
async def test_resolve_identity_raises_lookup_error_on_connect_timeout():
    """SIMBAD transport timeouts must be distinguishable from a genuine no-match.

    Regression test: this used to return None on a timeout, which was indistinguishable
    from "SIMBAD was reached and has no record of this object" -- silently reporting a
    firewalled or unreachable network as UNRESOLVED. It must now raise SimbadLookupError
    so the resolver can report it as LOOKUP_FAILED instead."""
    client = _fake_client({})
    client.post.side_effect = httpx.ConnectTimeout(
        "connect timed out",
        request=httpx.Request("POST", "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"),
    )

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(SimbadLookupError):
            await resolve_identity("51 Peg")


@pytest.mark.asyncio
async def test_resolve_identity_raises_lookup_error_on_http_status_error():
    """A bad HTTP status (e.g. SIMBAD returning a 503) is also a service-reachability
    problem, not a genuine no-match, and must raise the same way as a timeout."""
    request = httpx.Request("POST", "https://simbad.cds.unistra.fr/simbad/sim-tap/sync")
    response = MagicMock()
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("service unavailable", request=request, response=MagicMock(status_code=503))
    )

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    client.post = AsyncMock(return_value=response)

    with patch("httpx.AsyncClient", return_value=client):
        with pytest.raises(SimbadLookupError):
            await resolve_identity("51 Peg")


@pytest.mark.asyncio
async def test_resolve_identity_does_not_flag_truncation_at_exactly_ten_candidates():
    """Exactly at the display cap (10) is a complete list, not a truncated one --
    must not be flagged."""
    rows = [[f"Candidate {i}", 10.0 + i, 20.0, "Star", "G2V", ""] for i in range(10)]
    with patch("httpx.AsyncClient", return_value=_fake_client(_tap_envelope(rows))):
        candidates = await resolve_identity("Ambiguous Name")

    assert len(candidates) == 10
    assert all(not c.get("candidates_truncated") for c in candidates)


@pytest.mark.asyncio
async def test_resolve_identity_flags_truncation_above_ten_candidates():
    """Regression test for EVALUATION.md 1.6: when SIMBAD's TOP 11 query (one more
    than the display cap) comes back full, more than 10 objects genuinely matched.
    The result must be truncated to 10 *and* every returned candidate flagged, so
    the UI can tell the user the list is incomplete instead of silently presenting
    it as exhaustive."""
    rows = [[f"Candidate {i}", 10.0 + i, 20.0, "Star", "G2V", ""] for i in range(11)]
    with patch("httpx.AsyncClient", return_value=_fake_client(_tap_envelope(rows))):
        candidates = await resolve_identity("Very Ambiguous Name")

    assert len(candidates) == 10
    assert all(c.get("candidates_truncated") is True for c in candidates)


@pytest.mark.asyncio
async def test_resolve_identity_query_requests_one_more_row_than_the_display_cap():
    """The ADQL query itself must ask for 11 rows (TOP 10 + 1), not just 10 -- that
    extra row is what makes truncation detectable at all."""
    client = _fake_client(_tap_envelope([]))
    with patch("httpx.AsyncClient", return_value=client):
        await resolve_identity("51 Peg")

    sent_query = client.post.await_args.kwargs["data"]["query"]
    assert "TOP 11" in sent_query


@pytest.mark.asyncio
async def test_resolve_identity_deduplicates_candidates_by_main_id():
    """P2-6: two rows sharing one main_id (matched via different aliases) must
    collapse into a single candidate, not appear twice in the AMBIGUOUS list."""
    rows = [
        ["51 Peg", 344.36, 20.77, "Star", "G2V", "HD 217014|51 Peg"],
        ["51 Peg", 344.36, 20.77, "Star", "G2V", "HD 217014|51 Peg"],
        ["51 Peg B", 344.37, 20.78, "Star", "M4V", "HD 217014 B"],
    ]
    with patch("httpx.AsyncClient", return_value=_fake_client(_tap_envelope(rows))):
        result = await resolve_identity("51 Peg")

    assert isinstance(result, list)
    assert len(result) == 2
    assert {c["main_id"] for c in result} == {"51 Peg", "51 Peg B"}


@pytest.mark.asyncio
async def test_resolve_identity_dedup_collapses_to_single_dict_when_only_one_unique():
    """If every row shares the same main_id, de-duplication should leave exactly
    one candidate, which then hits the single-candidate short-circuit."""
    rows = [
        ["51 Peg", 344.36, 20.77, "Star", "G2V", "HD 217014|51 Peg"],
        ["51 Peg", 344.36, 20.77, "Star", "G2V", "HD 217014|51 Peg"],
    ]
    with patch("httpx.AsyncClient", return_value=_fake_client(_tap_envelope(rows))):
        result = await resolve_identity("51 Peg")

    assert isinstance(result, dict)
    assert result["main_id"] == "51 Peg"


@pytest.mark.asyncio
async def test_resolve_identity_rejects_structurally_hostile_query():
    """P2-4: a query containing disallowed characters (e.g. parentheses/semicolons)
    must be rejected before being sent upstream, rather than interpolated into the
    ADQL query string."""
    client = _fake_client(_tap_envelope([]))
    with patch("httpx.AsyncClient", return_value=client):
        result = await resolve_identity("51 Peg'); DROP TABLE basic; --")

    assert result is None
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_identity_allows_legitimate_identifiers_with_special_chars():
    """The allow-list must not reject real designations used elsewhere in the app
    (e.g. the README's own result examples)."""
    client = _fake_client(_tap_envelope([]))
    with patch("httpx.AsyncClient", return_value=client):
        for identifier in ["51 Peg", "HD 217014", "GJ 667 C", "TYC 1949-2020-1", "Barnard's Star"]:
            await resolve_identity(identifier)

    assert client.post.await_count == 5
