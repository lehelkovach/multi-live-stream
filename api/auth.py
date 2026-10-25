"""Single-admin authentication: argon2 password verification + signed session cookie.

* Password: ``OMNI_ADMIN_PASSWORD_HASH`` (argon2 PHC string, preferred) or
  ``OMNI_ADMIN_PASSWORD`` (hashed in memory at startup; the plaintext is never
  stored anywhere else).
* Session: an ``itsdangerous`` signed + timestamped token in an HttpOnly cookie.
  Changing ``OMNI_SESSION_SECRET`` invalidates every session.
* Brute force: a small in-memory limiter per client IP on the login endpoint.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.settings import Settings

_hasher = PasswordHasher()  # argon2id, library defaults (OWASP-aligned)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


class PasswordVerifier:
    """Verifies the admin password without ever exposing the plaintext."""

    def __init__(self, username: str, password_hash: str) -> None:
        self.username = username
        self._hash = password_hash

    @classmethod
    def from_settings(cls, settings: Settings) -> PasswordVerifier:
        pw_hash = settings.admin_password_hash.get_secret_value().strip()
        plain = settings.admin_password.get_secret_value()
        if not pw_hash and plain:
            pw_hash = hash_password(plain)
        if not pw_hash:
            msg = "set OMNI_ADMIN_PASSWORD_HASH (make hash-password) or OMNI_ADMIN_PASSWORD"
            raise ValueError(msg)
        return cls(settings.admin_username, pw_hash)

    def verify(self, username: str, password: str) -> bool:
        user_ok = hmac.compare_digest(username.encode(), self.username.encode())
        pw_ok: bool
        try:
            pw_ok = _hasher.verify(self._hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pw_ok = False
        return user_ok and pw_ok


@dataclass
class SessionSigner:
    secret: str
    max_age: int
    _serializer: URLSafeTimedSerializer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._serializer = URLSafeTimedSerializer(self.secret, salt="omni-session")

    def issue(self, username: str) -> str:
        return str(self._serializer.dumps({"u": username}))

    def read(self, token: str | None) -> str | None:
        """Return the username for a valid, unexpired token, else ``None``."""
        if not token:
            return None
        try:
            data = self._serializer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return None
        user = data.get("u") if isinstance(data, dict) else None
        return user if isinstance(user, str) and user else None


class LoginRateLimiter:
    """Sliding-window limiter: at most ``limit`` attempts per ``window`` seconds per key."""

    def __init__(self, limit: int = 10, window: float = 300.0) -> None:
        self.limit = limit
        self.window = window
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._attempts.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._attempts) > 10_000:  # bound memory under abuse
                self._attempts = {k: v for k, v in self._attempts.items() if v}
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


def cookie_secure_flag(settings: Settings, request: Request) -> bool:
    if settings.cookie_secure == "always":
        return True
    if settings.cookie_secure == "never":
        return False
    return request.url.scheme == "https"


def set_session_cookie(
    response: Response, request: Request, settings: Settings, token: str
) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_max_age,
        httponly=True,
        secure=cookie_secure_flag(settings, request),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, request: Request, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=cookie_secure_flag(settings, request),
        samesite="lax",
    )
