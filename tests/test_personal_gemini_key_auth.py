"""Tests for the personal-Gemini-key fallback settings in app/auth.py (README
section III.A): app.auth.update_personal_gemini_settings() and
app.auth.get_personal_gemini_key().
"""

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./astronomy_test_cache.db")

import pytest
import pytest_asyncio

from app import auth as auth_mod
from app.database import engine, init_db
from app.models import Base


@pytest_asyncio.fixture(autouse=True)
async def _init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield


@pytest.mark.asyncio
async def test_save_then_retrieve_personal_key_round_trips():
    user = await auth_mod.create_user("wolfie@example.com", "hunter22")

    await auth_mod.update_personal_gemini_settings(
        user.id, api_key="AIza-fake-personal-key", preferred_model="gemini-2.5-pro", clear=False
    )

    api_key, preferred_model = await auth_mod.get_personal_gemini_key(user.id)
    assert api_key == "AIza-fake-personal-key"
    assert preferred_model == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_no_personal_key_returns_none_none():
    user = await auth_mod.create_user("wolfie@example.com", "hunter22")

    api_key, preferred_model = await auth_mod.get_personal_gemini_key(user.id)
    assert api_key is None
    assert preferred_model is None


@pytest.mark.asyncio
async def test_stored_key_is_not_plaintext_in_the_database():
    user = await auth_mod.create_user("wolfie@example.com", "hunter22")
    await auth_mod.update_personal_gemini_settings(
        user.id, api_key="AIza-fake-personal-key", preferred_model=None, clear=False
    )

    refreshed = await auth_mod.get_user_by_id(user.id)
    assert refreshed.gemini_api_key_encrypted is not None
    assert refreshed.gemini_api_key_encrypted != "AIza-fake-personal-key"


@pytest.mark.asyncio
async def test_clear_removes_both_key_and_preferred_model():
    user = await auth_mod.create_user("wolfie@example.com", "hunter22")
    await auth_mod.update_personal_gemini_settings(
        user.id, api_key="AIza-fake-personal-key", preferred_model="gemini-2.5-pro", clear=False
    )

    await auth_mod.update_personal_gemini_settings(user.id, api_key=None, preferred_model=None, clear=True)

    api_key, preferred_model = await auth_mod.get_personal_gemini_key(user.id)
    assert api_key is None
    assert preferred_model is None


@pytest.mark.asyncio
async def test_updating_preferred_model_alone_leaves_existing_key_intact():
    """A user must be able to change their preferred model without re-entering
    the key, since the key is never decrypted back for display."""
    user = await auth_mod.create_user("wolfie@example.com", "hunter22")
    await auth_mod.update_personal_gemini_settings(
        user.id, api_key="AIza-fake-personal-key", preferred_model="gemini-2.5-pro", clear=False
    )

    await auth_mod.update_personal_gemini_settings(
        user.id, api_key=None, preferred_model="gemini-3.0-pro", clear=False
    )

    api_key, preferred_model = await auth_mod.get_personal_gemini_key(user.id)
    assert api_key == "AIza-fake-personal-key"
    assert preferred_model == "gemini-3.0-pro"


@pytest.mark.asyncio
async def test_overlong_api_key_is_rejected_without_writing_anything():
    user = await auth_mod.create_user("wolfie@example.com", "hunter22")

    with pytest.raises(ValueError):
        await auth_mod.update_personal_gemini_settings(
            user.id, api_key="x" * 500, preferred_model=None, clear=False
        )

    api_key, _ = await auth_mod.get_personal_gemini_key(user.id)
    assert api_key is None
