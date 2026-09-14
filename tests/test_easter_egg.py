"""Don't look"""

from unittest.mock import AsyncMock, patch
import pytest

from app.resolver import get_easter_egg, resolve_query


@pytest.mark.asyncio
async def test_get_easter_egg_lookup():
    """Verify that get_easter_egg matches queries case-insensitively."""
    amphoreus_egg = get_easter_egg("Amphoreus")
    assert amphoreus_egg is not None
    assert amphoreus_egg["target_name"] == "Amphoreus"
    assert "https://honkai-star-rail.fandom.com/wiki/Amphoreus" in amphoreus_egg["url"]

    penacony_egg = get_easter_egg("Land of the Dreams, Penacony")
    assert penacony_egg is not None
    assert penacony_egg["target_name"] == "Penacony"
    assert "https://honkai-star-rail.fandom.com/wiki/Penacony" in penacony_egg["url"]

    assert get_easter_egg("Sirius") is None


@pytest.mark.asyncio
@patch("app.resolver.resolve_identity")
@patch("app.resolver.find_planets")
async def test_resolve_query_easter_egg_bypasses_external_apis(mock_find_planets, mock_resolve_identity):
    """Verify that easter egg queries immediately return UNRESOLVED without calling SIMBAD/NASA."""
    terms = [
        "Amphoreus",
        "Amphoreus, the Eternal Land",
        "The Eternal Land, Amphoreus",
        "Penacony",
        "Land of the Dreams, Penacony",
        "Penacony, Land of the Dreams",
    ]

    for term in terms:
        res = await resolve_query(term)
        assert res.state == "UNRESOLVED"
        assert res.query_text is not None

    mock_resolve_identity.assert_not_called()
    mock_find_planets.assert_not_called()
