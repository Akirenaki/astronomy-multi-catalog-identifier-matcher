"""Route-level tests for /account/settings (README section III.A: personal
Gemini API key fallback) and its wiring into the generate/regenerate summary
routes.
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest_asyncio
from fastapi.testclient import TestClient

from app.database import engine, init_db
from app.main import app
from app.models import Base

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


def _register(client) -> str:
    csrf_token = get_csrf_token(client)
    client.post(
        "/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token}
    )
    return get_csrf_token(client)


def _mock_betelgeuse(monkeypatch):
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


def test_anonymous_get_redirects_to_login():
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/account/settings")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/account/settings"


def test_anonymous_post_redirects_to_login():
    with TestClient(app, follow_redirects=False) as client:
        csrf_token = get_csrf_token(client)
        response = client.post(
            "/account/settings", data={"csrf_token": csrf_token, "api_key": "AIza-fake"}
        )
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/account/settings"


def test_settings_page_shows_no_key_saved_initially():
    with TestClient(app) as client:
        csrf_token = _register(client)
        response = client.get("/account/settings")
    assert response.status_code == 200
    assert "No personal key saved." in response.text


def test_saving_a_key_updates_status_and_never_echoes_the_key():
    with TestClient(app) as client:
        csrf_token = _register(client)
        response = client.post(
            "/account/settings",
            data={"csrf_token": csrf_token, "api_key": "AIzaSy-super-secret-key-value", "preferred_model": ""},
        )

    assert response.status_code == 200
    assert "A personal key is currently saved." in response.text
    assert "AIzaSy-super-secret-key-value" not in response.text


def test_clearing_a_key_removes_it():
    with TestClient(app) as client:
        csrf_token = _register(client)
        client.post(
            "/account/settings", data={"csrf_token": csrf_token, "api_key": "AIzaSy-super-secret-key-value"}
        )
        csrf_token = get_csrf_token(client)
        response = client.post("/account/settings", data={"csrf_token": csrf_token, "clear_key": "on"})

    assert response.status_code == 200
    assert "No personal key saved." in response.text
    assert "Personal API key removed." in response.text


def test_overlong_key_is_rejected_with_400():
    with TestClient(app) as client:
        csrf_token = _register(client)
        response = client.post(
            "/account/settings", data={"csrf_token": csrf_token, "api_key": "x" * 500}
        )

    assert response.status_code == 400
    assert "No personal key saved." in response.text  # nothing was written


def test_overlong_preferred_model_is_rejected_with_400():
    with TestClient(app) as client:
        csrf_token = _register(client)
        response = client.post(
            "/account/settings",
            data={"csrf_token": csrf_token, "preferred_model": "x" * 500},
        )

    assert response.status_code == 400
    assert "No personal key saved." in response.text  # nothing was written


def test_missing_csrf_token_is_rejected():
    with TestClient(app) as client:
        _register(client)
        response = client.post("/account/settings", data={"api_key": "AIzaSy-fake"})
    assert response.status_code == 422  # FastAPI's Form(...) validation error for the missing field


def test_generate_route_passes_personal_key_to_ensure_ai_summary(monkeypatch):
    """End-to-end wiring check: the /summary route must fetch the logged-in
    user's personal key and pass it through to ensure_ai_summary()."""
    _mock_betelgeuse(monkeypatch)
    encoded_id = quote("* alf Ori", safe="")

    captured_kwargs = {}

    async def fake_ensure_ai_summary(simbad_main_id, *, personal_api_key=None, personal_model=None):
        captured_kwargs["personal_api_key"] = personal_api_key
        captured_kwargs["personal_model"] = personal_model
        return "A summary."

    monkeypatch.setattr("app.main.ensure_ai_summary", fake_ensure_ai_summary)

    with TestClient(app) as client:
        csrf_token = _register(client)
        client.post(
            "/account/settings",
            data={"csrf_token": csrf_token, "api_key": "AIza-my-key", "preferred_model": "gemini-2.5-pro"},
        )
        csrf_token = get_csrf_token(client)
        client.get("/search?q=Betelgeuse")

        response = client.post(
            f"/object/{encoded_id}/summary", headers={"X-CSRF-Token": csrf_token}
        )

    assert response.status_code == 200
    assert captured_kwargs["personal_api_key"] == "AIza-my-key"
    assert captured_kwargs["personal_model"] == "gemini-2.5-pro"


def test_regenerate_route_passes_personal_key_to_regenerate_ai_summary(monkeypatch):
    _mock_betelgeuse(monkeypatch)
    encoded_id = quote("* alf Ori", safe="")

    captured_kwargs = {}

    async def fake_regenerate_ai_summary(simbad_main_id, *, personal_api_key=None, personal_model=None):
        captured_kwargs["personal_api_key"] = personal_api_key
        captured_kwargs["personal_model"] = personal_model
        return "A regenerated summary."

    monkeypatch.setattr("app.main.regenerate_ai_summary", fake_regenerate_ai_summary)

    with TestClient(app) as client:
        csrf_token = _register(client)
        client.post(
            "/account/settings",
            data={"csrf_token": csrf_token, "api_key": "AIza-my-key", "preferred_model": "gemini-2.5-pro"},
        )
        csrf_token = get_csrf_token(client)
        client.get("/search?q=Betelgeuse")

        response = client.post(
            f"/object/{encoded_id}/summary/regenerate", headers={"X-CSRF-Token": csrf_token}
        )

    assert response.status_code == 200
    assert captured_kwargs["personal_api_key"] == "AIza-my-key"
    assert captured_kwargs["personal_model"] == "gemini-2.5-pro"


def test_anonymous_generate_route_does_not_look_up_a_personal_key(monkeypatch):
    """No current_user -> no personal key lookup, and both kwargs stay None."""
    _mock_betelgeuse(monkeypatch)
    encoded_id = quote("* alf Ori", safe="")

    captured_kwargs = {}

    async def fake_ensure_ai_summary(simbad_main_id, *, personal_api_key=None, personal_model=None):
        captured_kwargs["personal_api_key"] = personal_api_key
        captured_kwargs["personal_model"] = personal_model
        return "A summary."

    monkeypatch.setattr("app.main.ensure_ai_summary", fake_ensure_ai_summary)

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.get("/search?q=Betelgeuse")
        response = client.post(
            f"/object/{encoded_id}/summary", headers={"X-CSRF-Token": csrf_token}
        )

    assert response.status_code == 200
    assert captured_kwargs["personal_api_key"] is None
    assert captured_kwargs["personal_model"] is None
