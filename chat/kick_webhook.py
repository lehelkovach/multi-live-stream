"""Kick webhook signature verification (docs.kick.com/events/webhook-security).

Signed string: ``<Kick-Event-Message-Id>.<Kick-Event-Message-Timestamp>.<raw body>``,
RSA PKCS#1 v1.5 with SHA-256, signature base64 in ``Kick-Event-Signature``. The
public key comes from ``GET https://api.kick.com/public/v1/public-key`` and may
rotate, so it is fetched (and cached) rather than hard-coded.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
HEADER_MESSAGE_ID = "Kick-Event-Message-Id"
HEADER_TIMESTAMP = "Kick-Event-Message-Timestamp"
HEADER_SIGNATURE = "Kick-Event-Signature"
HEADER_TYPE = "Kick-Event-Type"
HEADER_VERSION = "Kick-Event-Version"
HEADER_SUBSCRIPTION = "Kick-Event-Subscription-Id"


def signed_payload(message_id: str, timestamp: str, body: bytes) -> bytes:
    return f"{message_id}.{timestamp}.".encode() + body


def verify_signature(
    public_key_pem: str | bytes, message_id: str, timestamp: str, body: bytes, signature_b64: str
) -> bool:
    pem = public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem
    try:
        key = serialization.load_pem_public_key(pem)
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError):
        return False
    if not isinstance(key, rsa.RSAPublicKey):
        return False
    try:
        key.verify(
            signature,
            signed_payload(message_id, timestamp, body),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature:
        return False
    return True


def sign_payload(private_key_pem: bytes, message_id: str, timestamp: str, body: bytes) -> str:
    """Test helper: produce a signature the way Kick does."""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    assert isinstance(key, rsa.RSAPrivateKey)
    sig = key.sign(signed_payload(message_id, timestamp, body), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode()


class PublicKeyCache:
    """Fetches Kick's public key lazily and re-fetches after ``ttl`` or on demand."""

    def __init__(
        self, url: str = PUBLIC_KEY_URL, ttl: float = 3600.0, *, pinned: str | None = None
    ) -> None:
        self.url = url
        self.ttl = ttl
        self._pem: str | None = pinned
        self._fetched_at = time.monotonic() if pinned else 0.0
        self._pinned = pinned is not None

    async def get(self, http: httpx.AsyncClient, *, force: bool = False) -> str | None:
        if self._pinned:
            return self._pem
        if self._pem and not force and time.monotonic() - self._fetched_at < self.ttl:
            return self._pem
        try:
            resp = await http.get(self.url, timeout=10)
            resp.raise_for_status()
            data: Any = resp.json()
        except (httpx.HTTPError, ValueError):
            return self._pem
        pem = None
        if isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else data
            pem = inner.get("public_key") if isinstance(inner, dict) else None
        if isinstance(pem, str) and "PUBLIC KEY" in pem:
            self._pem = pem
            self._fetched_at = time.monotonic()
        return self._pem
