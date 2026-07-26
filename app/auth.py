"""Auth helpers."""
from __future__ import annotations

import secrets

import bcrypt
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.models import User

_MAX_PASSWORD_BYTES = 72
_MIN_PASSWORD_LENGTH = 8

# A fixed, valid bcrypt hash with no corresponding real password. Used solely so
# that authenticate() can run a checkpw() call of realistic cost on the
# "no such user" path -- see authenticate()'s docstring below for why.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"dummy-password-for-timing", bcrypt.gensalt()).decode("utf-8")


class DuplicateEmailError(Exception):
    """Raised when an email is already registered."""


def hash_password(password: str) -> str:
    """Hash a password for storage."""
    if len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError("Password too long")
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Check a password against a stored hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


async def create_user(email: str, password: str) -> User:
    """Create a new user."""
    if not password:
        raise ValueError("Password required")
    password_hash = hash_password(password)

    async with SessionLocal() as session:
        user = User(email=email, password_hash=password_hash)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise DuplicateEmailError(f"Email already registered: {email!r}")
        await session.refresh(user)
        return user


async def get_user_by_email(email: str) -> User | None:
    """Look up a user by email."""
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()


async def get_user_by_id(user_id: int) -> User | None:
    """Look up a user by id."""
    async with SessionLocal() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()


async def authenticate(email: str, password: str) -> User | None:
    """Verify credentials.

    Always performs a bcrypt.checkpw()-equivalent comparison, even when the
    email doesn't exist, by checking the supplied password against a fixed
    dummy hash in that case. Without this, an unknown email would return
    immediately (no bcrypt call), while a known email always pays bcrypt's
    ~O(100ms) cost -- a timing side-channel an attacker could use to enumerate
    registered emails.
    """
    user = await get_user_by_email(email)
    if user is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def log_in_session(request: Request, user: User) -> None:
    """Record the current user in the session."""
    request.session["user_id"] = user.id


def log_out_session(request: Request) -> None:
    """Clear the current user from the session."""
    request.session.pop("user_id", None)


async def get_current_user(request: Request) -> User | None:
    """Return the logged-in user for the current session."""
    user_id = request.session.get("user_id")
    if user_id is None:
        return None
    user = await get_user_by_id(user_id)
    if user is None:
        log_out_session(request)
    return user


def get_session_id(request: Request) -> str:
    """Return a stable visitor identifier."""
    session_id = request.session.get("session_id")
    if session_id is None:
        session_id = secrets.token_hex(16)
        request.session["session_id"] = session_id
    return session_id


def get_csrf_token(request: Request) -> str:
    """Return this session's CSRF token."""
    token = request.session.get("csrf_token")
    if token is None:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf_token(request: Request, submitted_token: str | None) -> bool:
    """Check a submitted CSRF token."""
    expected = request.session.get("csrf_token")
    if not expected or not submitted_token:
        return False
    return secrets.compare_digest(expected, submitted_token)
