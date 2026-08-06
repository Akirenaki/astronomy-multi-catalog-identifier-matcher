"""Fernet-backed helpers for per-user secret storage."""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from app.config import load_environment

logger = logging.getLogger(__name__)

load_environment()


def _load_fernet() -> Fernet:
    raw_key = os.getenv("USER_SECRET_ENCRYPTION_KEY")
    if raw_key:
        try:
            return Fernet(raw_key.encode("utf-8"))
        except (ValueError, TypeError) as e:
            logger.error(
                "USER_SECRET_ENCRYPTION_KEY is set but is not a valid Fernet key (%s); "
                "falling back to a randomly generated per-process key. Any personal API "
                "keys encrypted under the previous key will fail to decrypt.",
                e,
            )
    else:
        logger.warning(
            "USER_SECRET_ENCRYPTION_KEY is not set; using a randomly generated key for "
            "this process only. Personal Gemini API keys saved under this key will "
            "become unreadable after a restart. Set USER_SECRET_ENCRYPTION_KEY explicitly "
            "before deploying anywhere beyond local single-process dev: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(Fernet.generate_key())


_fernet: Fernet = _load_fernet()


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext secret for storage."""
    return _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str | None:
    """Decrypt a stored secret.

    Returns None (rather than raising) if it can't be decrypted -- for
    example, if USER_SECRET_ENCRYPTION_KEY changed or was unset since the
    secret was originally encrypted under a different per-process random key.
    Callers should treat None the same as "no key saved" rather than erroring.
    """
    try:
        return _fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as e:
        logger.error("Failed to decrypt a stored personal API key: %s", e)
        return None
