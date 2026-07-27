"""Tests for app/crypto_utils.py, used to encrypt personal Gemini API keys at rest."""

from cryptography.fernet import Fernet

from app import crypto_utils


def test_encrypt_then_decrypt_round_trips():
    plaintext = "AIzaSy-fake-personal-gemini-key-000000000"
    ciphertext = crypto_utils.encrypt_secret(plaintext)

    assert ciphertext != plaintext
    assert crypto_utils.decrypt_secret(ciphertext) == plaintext


def test_decrypt_returns_none_instead_of_raising_on_garbage_input():
    assert crypto_utils.decrypt_secret("not-a-valid-fernet-token") is None


def test_decrypt_returns_none_for_ciphertext_from_a_different_key():
    other_fernet = Fernet(Fernet.generate_key())
    ciphertext_from_other_key = other_fernet.encrypt(b"some-key").decode("utf-8")

    assert crypto_utils.decrypt_secret(ciphertext_from_other_key) is None
