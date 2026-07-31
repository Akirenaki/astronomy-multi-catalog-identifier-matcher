"""Tests for rate limiting on /login and /register (F3 / TICKET-102)."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest_asyncio
from fastapi.testclient import TestClient

from app import main as main_mod
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


def test_login_is_rate_limited_after_repeated_failed_attempts(monkeypatch):
    monkeypatch.setattr(main_mod, "AUTH_RATE_LIMIT", 3)

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token},
        )
        client.post("/logout", data={"csrf_token": csrf_token})
        csrf_token = get_csrf_token(client)

        last_response = None
        for _ in range(4):
            last_response = client.post(
                "/login",
                data={"email": "wolfie@example.com", "password": "wrong-password", "csrf_token": csrf_token},
            )

        assert last_response.status_code == 429


def test_successful_login_does_not_count_against_the_limit(monkeypatch):
    monkeypatch.setattr(main_mod, "AUTH_RATE_LIMIT", 3)

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token},
        )
        client.post("/logout", data={"csrf_token": csrf_token})
        csrf_token = get_csrf_token(client)

        for _ in range(5):
            response = client.post(
                "/login",
                data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token},
            )
            assert response.status_code in (200, 303, 400)
            if response.status_code in (200, 303):
                client.post("/logout", data={"csrf_token": csrf_token})
                csrf_token = get_csrf_token(client)


def test_register_is_rate_limited_after_repeated_attempts(monkeypatch):
    monkeypatch.setattr(main_mod, "AUTH_RATE_LIMIT", 3)

    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)

        last_response = None
        for i in range(4):
            last_response = client.post(
                "/register",
                data={"email": f"wolfie{i}@example.com", "password": "hunter22", "csrf_token": csrf_token},
            )

        assert last_response.status_code == 429
