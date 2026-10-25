"""Encryption at rest for credentials (stream keys, OAuth tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256) via ``MultiFernet`` so keys can be rotated:
the first key encrypts, every key decrypts. Keys come from ``OMNI_SECRET_KEY``.
"""

from __future__ import annotations

import base64
import secrets

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

__all__ = ["DecryptError", "SecretBox", "generate_key"]


class DecryptError(ValueError):
    """Raised when a ciphertext cannot be decrypted with any configured key."""


def generate_key() -> str:
    """Return a fresh urlsafe-base64 32-byte key suitable for ``OMNI_SECRET_KEY``."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


class SecretBox:
    """Encrypt/decrypt short strings with one or more Fernet keys."""

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            msg = "at least one encryption key is required"
            raise ValueError(msg)
        try:
            self._fernet = MultiFernet([Fernet(k.encode("ascii")) for k in keys])
        except (ValueError, TypeError) as exc:
            msg = "OMNI_SECRET_KEY must be urlsafe-base64 32-byte key(s) (run `make secret`)"
            raise ValueError(msg) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            msg = "ciphertext is invalid or was encrypted with an unknown key"
            raise DecryptError(msg) from exc

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt ``ciphertext`` with the primary key (no-op if already current)."""
        return self._fernet.rotate(ciphertext.encode("ascii")).decode("ascii")
