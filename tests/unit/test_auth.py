from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.auth import LoginRateLimiter, PasswordVerifier, SessionSigner, hash_password
from core.crypto import generate_key
from core.settings import Settings
from tests.conftest import ADMIN_PASSWORD, ADMIN_USER


def test_login_sets_cookie_and_me_works(client: TestClient, settings: Settings) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401
    resp = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json() == {"username": ADMIN_USER}
    cookie = resp.headers["set-cookie"]
    assert settings.session_cookie_name in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert client.get("/api/v1/auth/me").json() == {"username": ADMIN_USER}


def test_wrong_password_rejected(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/login", json={"username": ADMIN_USER, "password": "nope"})
    assert resp.status_code == 401
    assert client.get("/api/v1/auth/me").status_code == 401


def test_wrong_username_rejected(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/login", json={"username": "root", "password": ADMIN_PASSWORD})
    assert resp.status_code == 401


def test_logout_clears_session(auth_client: TestClient) -> None:
    assert auth_client.get("/api/v1/auth/me").status_code == 200
    assert auth_client.post("/api/v1/auth/logout").status_code == 200
    assert auth_client.get("/api/v1/auth/me").status_code == 401


def test_tampered_cookie_rejected(auth_client: TestClient, settings: Settings) -> None:
    token = auth_client.cookies[settings.session_cookie_name]
    auth_client.cookies.set(settings.session_cookie_name, token[:-3] + "abc")
    assert auth_client.get("/api/v1/auth/me").status_code == 401


def test_login_rate_limited(client: TestClient) -> None:
    for _ in range(10):
        client.post("/api/v1/auth/login", json={"username": ADMIN_USER, "password": "bad"})
    resp = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 429


def test_validation_errors(client: TestClient) -> None:
    bad = client.post("/api/v1/auth/login", json={"username": "", "password": "x"})
    assert bad.status_code == 422
    assert client.post("/api/v1/auth/login", json={}).status_code == 422


# --- unit level ---------------------------------------------------------------


def test_password_verifier_from_plaintext_or_hash() -> None:
    base = {"secret_key": generate_key(), "session_secret": "x" * 32}
    v = PasswordVerifier.from_settings(Settings(admin_password="pw12345678", **base))  # type: ignore[arg-type]
    assert v.verify("admin", "pw12345678")
    assert not v.verify("admin", "pw1234567")
    v2 = PasswordVerifier.from_settings(
        Settings(admin_password_hash=hash_password("abc"), **base)  # type: ignore[arg-type]
    )
    assert v2.verify("admin", "abc")
    assert not v2.verify("Admin", "abc")
    with pytest.raises(ValueError, match="OMNI_ADMIN_PASSWORD"):
        PasswordVerifier.from_settings(Settings(**base))  # type: ignore[arg-type]


def test_verifier_tolerates_garbage_hash() -> None:
    assert not PasswordVerifier("admin", "not-a-hash").verify("admin", "x")


def test_session_signer_roundtrip_and_expiry() -> None:
    signer = SessionSigner("s" * 32, max_age=60)
    token = signer.issue("admin")
    assert signer.read(token) == "admin"
    assert signer.read(None) is None
    assert signer.read("garbage") is None
    assert SessionSigner("other-secret-value-here-123456", 60).read(token) is None
    expired = SessionSigner("s" * 32, max_age=-1)
    assert expired.read(token) is None


def test_rate_limiter_window() -> None:
    rl = LoginRateLimiter(limit=3, window=10)
    assert all(rl.allow("ip", now=t) for t in (0, 1, 2))
    assert not rl.allow("ip", now=3)
    assert rl.allow("other", now=3)
    assert rl.allow("ip", now=11)  # first attempt (t=0) fell out of the window
    rl.reset("ip")
    assert rl.allow("ip", now=12)
