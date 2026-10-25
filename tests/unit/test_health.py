from __future__ import annotations

import time

import fakeredis
from fastapi.testclient import TestClient

from core.redis import KEY_RELAY_HEARTBEAT


def test_healthz_is_public(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["version"]


def test_detailed_health_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/health").status_code == 401


def test_detailed_health_reports_components(auth_client: TestClient) -> None:
    resp = auth_client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["components"]) == {"db", "redis", "mediamtx", "relay"}
    assert body["components"]["db"]["ok"] is True
    assert body["components"]["redis"]["ok"] is True
    assert body["components"]["mediamtx"]["ok"] is False  # nothing listening in tests
    assert body["components"]["relay"]["ok"] is False  # no heartbeat yet
    assert body["status"] == "degraded"


def test_relay_heartbeat_flips_relay_ok(
    auth_client: TestClient, redis_sync: fakeredis.FakeRedis
) -> None:
    redis_sync.set(KEY_RELAY_HEARTBEAT, str(time.time()))
    body = auth_client.get("/api/v1/health").json()
    assert body["components"]["relay"]["ok"] is True
    redis_sync.set(KEY_RELAY_HEARTBEAT, str(time.time() - 120))
    body = auth_client.get("/api/v1/health").json()
    assert body["components"]["relay"]["ok"] is False
