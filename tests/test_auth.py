"""Tests for Decision F: registration, login/logout, and the auth helpers they're
built on. See tests/test_favorites.py and tests/test_summary_snapshots.py for the
routes that actually require a logged-in user."""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app import auth as auth_mod
from app.database import engine, init_db
from app.main import app
from app.models import Base

from conftest import get_csrf_token


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    """Same isolation rationale as the rest of the suite -- see test_cache.py."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


# --- app.auth unit tests -----------------------------------------------------

@pytest.mark.asyncio
async def test_create_user_hashes_password_not_plaintext():
    """The stored password_hash must not be the plaintext password."""
    user = await auth_mod.create_user(email="wolfie@example.com", password="hunter22")
    assert user.password_hash != "hunter22"
    assert auth_mod.verify_password("hunter22", user.password_hash) is True


def test_verify_password_rejects_wrong_password():
    """verify_password is synchronous -- confirm it actually distinguishes right
    from wrong passwords rather than always returning True."""
    password_hash = auth_mod.hash_password("correct-horse")
    assert auth_mod.verify_password("correct-horse", password_hash) is True
    assert auth_mod.verify_password("wrong-guess", password_hash) is False


@pytest.mark.asyncio
async def test_create_user_duplicate_email_raises_cleanly():
    """Registering the same email twice must raise DuplicateEmailError (caught by
    the /register route to re-render a friendly error), not crash with a raw
    IntegrityError/500."""
    await auth_mod.create_user(email="wolfie@example.com", password="first-password")
    with pytest.raises(auth_mod.DuplicateEmailError):
        await auth_mod.create_user(email="wolfie@example.com", password="second-password")


@pytest.mark.asyncio
async def test_authenticate_returns_none_for_unknown_email():
    """Sanity check on the behaviour, independent of the timing fix below."""
    result = await auth_mod.authenticate("nobody@example.com", "whatever")
    assert result is None


@pytest.mark.asyncio
async def test_authenticate_runs_bcrypt_check_even_for_unknown_email(monkeypatch):
    """Regression test for F9/TICKET-07: authenticating against an email that
    doesn't exist must still call verify_password() (and therefore pay bcrypt's
    cost), rather than returning immediately -- otherwise the login endpoint's
    response time leaks whether a given email is registered."""
    calls = []
    real_verify_password = auth_mod.verify_password

    def spy_verify_password(password, password_hash):
        calls.append(password_hash)
        return real_verify_password(password, password_hash)

    monkeypatch.setattr(auth_mod, "verify_password", spy_verify_password)

    result = await auth_mod.authenticate("nobody@example.com", "whatever")

    assert result is None
    assert len(calls) == 1
    # It must have checked against the fixed dummy hash, not skipped the call.
    assert calls[0] == auth_mod._DUMMY_PASSWORD_HASH


@pytest.mark.asyncio
async def test_authenticate_succeeds_for_correct_known_credentials():
    """Companion test: the known-user path must still work correctly."""
    await auth_mod.create_user(email="wolfie@example.com", password="hunter22")
    result = await auth_mod.authenticate("wolfie@example.com", "hunter22")
    assert result is not None
    assert result.email == "wolfie@example.com"


# --- route-level tests --------------------------------------------------------

def test_register_route_creates_account_and_logs_in():
    """POST /register should create the user and leave the session logged in --
    the next request in the same client should be recognized as that user."""
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        response = client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303

        home_response = client.get("/")

    assert "wolfie@example.com" in home_response.text


def test_register_route_rejects_duplicate_email_with_400_not_500():
    """The doc's explicit regression case: a duplicate-email registration must fail
    cleanly with a 400 and a visible error message, not a raw 500."""
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token})
        second = client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "different-password", "csrf_token": csrf_token},
            follow_redirects=False,
        )

    assert second.status_code == 400
    assert "already registered" in second.text


def test_login_route_sets_session_and_protected_route_succeeds():
    """Login sets a session cookie; a protected route (here, /account/saved)
    succeeds after login."""
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token})
        client.post("/logout", data={"csrf_token": csrf_token})

        # Confirm the protected route rejects/redirects before login.
        before_login = client.get("/account/saved", follow_redirects=False)
        assert before_login.status_code == 303
        assert before_login.headers["location"].startswith("/login")

        login_response = client.post(
            "/login",
            data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert login_response.status_code == 303

        after_login = client.get("/account/saved")

    assert after_login.status_code == 200


def test_login_route_rejects_wrong_password():
    """A wrong password must not log the user in."""
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token})
        client.post("/logout", data={"csrf_token": csrf_token})

        response = client.post(
            "/login",
            data={"email": "wolfie@example.com", "password": "wrong-password", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 400

        protected = client.get("/account/saved", follow_redirects=False)

    assert protected.status_code == 303  # still anonymous


def test_logout_clears_session():
    """POST /logout must actually clear the logged-in state."""
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        client.post("/register", data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": csrf_token})
        client.post("/logout", data={"csrf_token": csrf_token})

        response = client.get("/account/saved", follow_redirects=False)

    assert response.status_code == 303


@pytest.mark.asyncio
async def test_create_user_rejects_password_below_minimum_length():
    """EVALUATION.md suggestion #7: a too-short password must be rejected with a
    clear ValueError, not silently accepted."""
    with pytest.raises(ValueError):
        await auth_mod.create_user(email="wolfie@example.com", password="short")


def test_register_route_rejects_short_password_with_400_not_500():
    """The route-level counterpart: a too-short password must 400 with a friendly
    message, not crash."""
    with TestClient(app) as client:
        csrf_token = get_csrf_token(client)
        response = client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "short", "csrf_token": csrf_token},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "at least" in response.text.lower()


def test_register_route_rejects_missing_csrf_token():
    """Regression test for EVALUATION.md suggestion #6: a POST /register with no
    csrf_token at all must be rejected, not processed."""
    with TestClient(app) as client:
        response = client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "hunter22"},
            follow_redirects=False,
        )

    assert response.status_code == 422  # FastAPI's Form(...) validation error for the missing field


def test_register_route_rejects_wrong_csrf_token():
    """A csrf_token that doesn't match this session's must also be rejected, not
    just an entirely absent one."""
    with TestClient(app) as client:
        get_csrf_token(client)  # mint a real session token, then deliberately ignore it
        response = client.post(
            "/register",
            data={"email": "wolfie@example.com", "password": "hunter22", "csrf_token": "not-the-real-token"},
            follow_redirects=False,
        )

    assert response.status_code == 403


def test_register_route_rejects_csrf_token_from_a_different_session():
    """A syntactically valid-looking token minted for a *different* session/user
    must not work here -- this is the actual cross-site attack CSRF protection
    exists to stop: an attacker can't just harvest their own valid token and
    replay it against a victim's session."""
    with TestClient(app) as attacker, TestClient(app) as victim:
        attackers_token = get_csrf_token(attacker)
        get_csrf_token(victim)

        response = victim.post(
            "/register",
            data={"email": "victim@example.com", "password": "hunter22", "csrf_token": attackers_token},
            follow_redirects=False,
        )

    assert response.status_code == 403
