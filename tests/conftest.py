"""Shared fixtures: isolated settings, an app with a temp SQLite DB and fake Redis."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import fakeredis
import fakeredis.aioredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.auth import hash_password
from core.crypto import generate_key
from core.settings import Settings, get_settings

ADMIN_USER = "admin"
ADMIN_PASSWORD = "correct horse battery staple"
INGEST_KEY = "test-ingest-key-0123456789"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure a developer's real .env / env vars never leak into tests."""
    for key in list(os.environ):
        if key.startswith("OMNI_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    get_settings.cache_clear()


@pytest.fixture
def admin_hash() -> str:
    return hash_password(ADMIN_PASSWORD)


@pytest.fixture
def settings(tmp_path: Path, admin_hash: str) -> Settings:
    return Settings(
        secret_key=generate_key(),
        session_secret="unit-test-session-secret-not-for-production",
        ingest_stream_key=INGEST_KEY,
        admin_username=ADMIN_USER,
        admin_password_hash=admin_hash,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        redis_url="redis://unused:6379/0",
        mediamtx_api_url="http://127.0.0.1:1",  # closed port => fast failure
        log_format="console",
        log_level="warning",
        cookie_secure="never",
        web_dist_dir=str(tmp_path / "no-dist"),
    )


@pytest.fixture
def fake_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def fake_redis(fake_server: fakeredis.FakeServer) -> fakeredis.aioredis.FakeRedis:
    """Async client used by the app."""
    return fakeredis.aioredis.FakeRedis(server=fake_server, decode_responses=True)


@pytest.fixture
def redis_sync(fake_server: fakeredis.FakeServer) -> fakeredis.FakeRedis:
    """Sync client sharing the same in-memory server, for assertions/poking in tests."""
    return fakeredis.FakeRedis(server=fake_server, decode_responses=True)


@pytest.fixture
def app(settings: Settings, fake_redis: fakeredis.aioredis.FakeRedis) -> FastAPI:
    from api.main import create_app

    application = create_app(settings)

    # Replace the real Redis client after lifespan startup creates it.
    original_lifespan = application.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(app):
            await app.state.redis.aclose()
            app.state.redis = fake_redis
            yield

    application.router.lifespan_context = patched_lifespan
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    resp = client.post(
        "/api/v1/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return client
