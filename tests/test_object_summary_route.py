"""Tests for the data-first loading UX: /search must render immediately without
waiting on Gemini, and /object/{id}/summary must lazily fill in the AI narrative
afterward. See app/templates/result.html's client-side script for the piece that
actually calls this route from the browser."""

import os

# Same isolation as test_cache.py: bind app.database's engine to a dedicated on-disk
# test database *before* app.main (and therefore app.database) is imported anywhere
# in this test session, so these tests never touch the app's real astronomy.db.
# Uses setdefault so whichever test file is collected first "wins" and every other
# file's engine points at the same isolated database for the rest of the run.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.database import engine, init_db
from app.main import app
from app.models import Base

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    """Give every test a genuinely empty database -- see test_cache.py's identical
    fixture for the full rationale (stale rows surviving between runs otherwise)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def test_search_renders_generate_button_without_calling_gemini(monkeypatch):
    """Core regression test for the opt-in AI UX (Decision A): /search must render
    the scientific data and a click-to-generate button for the AI summary WITHOUT
    waiting on (or calling) Gemini at all -- the summary is only fetched once the
    person explicitly clicks the button, via a separate request."""
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
    summary_mock = AsyncMock(return_value="should not be called during /search")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    with TestClient(app) as client:
        response = client.get("/search?q=Betelgeuse")

    assert response.status_code == 200
    assert "PARTIAL" in response.text
    # The generate button (and the data attribute the client-side JS reads to know
    # which object to fetch once clicked) must be present...
    assert 'id="ai-summary-panel"' in response.text
    assert 'id="generate-summary-btn"' in response.text
    assert 'data-simbad-id="* alf Ori"' in response.text
    # ...the loading placeholder must be hidden until the button is clicked...
    assert 'class="ai-summary-loading ai-summary-content" style="display:none;"' in response.text
    # ...and no Regenerate control should exist yet, since there's no summary at all.
    assert 'id="regenerate-summary-btn"' not in response.text
    # ...and Gemini must genuinely not have been called to produce this response.
    summary_mock.assert_not_awaited()


def test_object_summary_route_generates_and_returns_json(monkeypatch):
    """The route the frontend polls after /search has already rendered: given an
    object that was resolved without a summary yet, it should generate one, persist
    it, and return it as JSON."""
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

    with TestClient(app) as client:
        # First hit /search, mirroring the real flow: the page renders without a
        # summary yet, and only the follow-up request below generates one.
        search_response = client.get("/search?q=Betelgeuse")
        assert search_response.status_code == 200
        summary_mock.assert_not_awaited()

        summary_response = client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary",
            headers={"X-CSRF-Token": get_csrf_token(client)},
        )

    assert summary_response.status_code == 200
    assert summary_response.json() == {
        "summary": "Betelgeuse is a huge red star with no known planets.",
        "summary_html": "<p>Betelgeuse is a huge red star with no known planets.</p>\n",
    }
    summary_mock.assert_awaited_once()


def test_object_summary_route_returns_rendered_markdown(monkeypatch):
    """Markdown output from Gemini should render into HTML for the browser."""
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
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="Betelgeuse is **bright**."))

    with TestClient(app) as client:
        client.get("/search?q=Betelgeuse")
        summary_response = client.post(
            f"/object/{quote('* alf Ori', safe='')}/summary",
            headers={"X-CSRF-Token": get_csrf_token(client)},
        )
        profile_response = client.get(f"/object/{quote('* alf Ori', safe='')}")

    assert "<strong>bright</strong>" in summary_response.json()["summary_html"]
    assert "<strong>bright</strong>" in profile_response.text


def test_object_profile_renders_regenerate_button_when_summary_exists(monkeypatch):
    """Once ai_summary is already set on the record, result.html should render the
    summary text plus a server-side Regenerate button -- not the generate button."""
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
    monkeypatch.setattr("app.cache.generate_summary", AsyncMock(return_value="A supergiant star."))

    encoded_id = quote("* alf Ori", safe="")
    with TestClient(app) as client:
        client.get("/search?q=Betelgeuse")
        client.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": get_csrf_token(client)},
        )  # generates and persists the summary
        response = client.get(f"/object/{encoded_id}")

    assert response.status_code == 200
    assert "A supergiant star." in response.text
    assert 'id="regenerate-summary-btn"' in response.text
    assert 'id="generate-summary-btn"' not in response.text


def test_object_summary_route_returns_404_for_unknown_id():
    """A stale or mistyped id in the URL must produce a clean 404, not a 500."""
    with TestClient(app) as client:
        token = get_csrf_token(client)
        response = client.post(
            "/object/this-id-does-not-exist/summary",
            headers={"X-CSRF-Token": token},
        )

    assert response.status_code == 404


def test_object_summary_cache_hit_does_not_consume_rate_limit_quota(monkeypatch):
    """Regression test for EVALUATION.md 1.2: re-fetching an already-generated
    summary must not write a rate_limit_events row, since no Gemini call
    happens on that path. Set the limit to 1 so a second (unbudgeted) real
    generation would immediately 429 -- confirming the cache hit truly didn't
    spend the client's only slot."""
    from app import ratelimit as ratelimit_mod

    monkeypatch.setattr(ratelimit_mod, "AI_SUMMARY_RATE_LIMIT", 1)
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
    summary_mock = AsyncMock(return_value="A red supergiant.")
    monkeypatch.setattr("app.cache.generate_summary", summary_mock)

    encoded_id = quote("* alf Ori", safe="")
    with TestClient(app) as client:
        client.get("/search?q=Betelgeuse")
        first = client.post(
            f"/object/{encoded_id}/summary",
            headers={"X-CSRF-Token": get_csrf_token(client)},
        )  # generates, consumes the only slot
        assert first.status_code == 200
        summary_mock.assert_awaited_once()

        # A different object would 429 here (limit is 1 and it's already spent),
        # but repeated requests for the *same, already-summarized* object must
        # keep succeeding, because none of them should be charged against the
        # budget in the first place.
        for _ in range(3):
            again = client.post(
                f"/object/{encoded_id}/summary",
                headers={"X-CSRF-Token": get_csrf_token(client)},
            )
            assert again.status_code == 200
            assert again.json()["summary"] == "A red supergiant."

    # Gemini was still only ever called the one time, for the original generation.
    summary_mock.assert_awaited_once()
