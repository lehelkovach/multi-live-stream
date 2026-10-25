from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

from core.redis import KEY_RELAY_HEARTBEAT, dest_status_key

BASE = "/api/v1/destinations"
YT = {
    "name": "YouTube main",
    "platform": "youtube",
    "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
    "stream_key": "abcd-efgh-ijkl-mnop",
}


def test_requires_auth(client: TestClient) -> None:
    assert client.get(BASE).status_code == 401
    assert client.post(BASE, json=YT).status_code == 401


def test_create_list_get(auth_client: TestClient) -> None:
    resp = auth_client.post(BASE, json=YT)
    assert resp.status_code == 201, resp.text
    d = resp.json()
    assert d["id"] == 1
    assert d["enabled"] is True
    assert d["profile"] == "copy"
    assert d["status"] == "idle"
    assert d["runtime"] is None
    assert "stream_key" not in d
    assert d["stream_key_masked"] == "••••nop"
    assert auth_client.get(BASE).json()[0]["name"] == "YouTube main"
    assert auth_client.get(f"{BASE}/1").json()["rtmp_url"] == YT["rtmp_url"]
    assert auth_client.get(f"{BASE}/999").status_code == 404


def test_stream_key_is_encrypted_at_rest(auth_client: TestClient, settings) -> None:  # type: ignore[no-untyped-def]
    auth_client.post(BASE, json=YT)
    db_path = settings.database_url.replace("sqlite:///", "")
    assert Path(db_path).exists()
    raw = (
        sqlite3.connect(db_path)
        .execute("select stream_key_encrypted from destination")
        .fetchone()[0]
    )
    assert raw
    assert YT["stream_key"] not in raw
    assert raw.startswith("gAAAA")  # Fernet token


def test_validation(auth_client: TestClient) -> None:
    bad_url = {**YT, "rtmp_url": "http://example.com/live"}
    assert auth_client.post(BASE, json=bad_url).status_code == 422
    assert auth_client.post(BASE, json={**YT, "rtmp_url": "rtmp://"}).status_code == 422
    assert auth_client.post(BASE, json={**YT, "platform": "myspace"}).status_code == 422
    assert auth_client.post(BASE, json={**YT, "profile": "custom"}).status_code == 422  # needs args
    assert auth_client.post(BASE, json={**YT, "stream_key": "has space"}).status_code == 422
    assert auth_client.post(BASE, json={**YT, "name": ""}).status_code == 422
    ok = auth_client.post(BASE, json={**YT, "profile": "custom", "ffmpeg_args": "-c copy"})
    assert ok.status_code == 201


def test_update_toggle_and_key_handling(auth_client: TestClient) -> None:
    dest_id = auth_client.post(BASE, json=YT).json()["id"]
    r = auth_client.patch(f"{BASE}/{dest_id}", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert r.json()["status"] == "disabled"
    r = auth_client.patch(f"{BASE}/{dest_id}", json={"name": "Renamed"})
    assert r.json()["name"] == "Renamed"
    assert r.json()["stream_key_masked"] == "••••nop"  # unchanged
    r = auth_client.patch(f"{BASE}/{dest_id}", json={"stream_key": "newkey-123456"})
    assert r.json()["stream_key_masked"] == "••••456"
    r = auth_client.patch(f"{BASE}/{dest_id}", json={"stream_key": ""})
    assert r.json()["stream_key_masked"] == ""
    r = auth_client.patch(f"{BASE}/{dest_id}", json={"profile": "custom"})
    assert r.status_code == 422
    r = auth_client.patch(f"{BASE}/{dest_id}", json={"profile": "vertical_720p"})
    assert r.json()["profile"] == "vertical_720p"
    assert auth_client.patch(f"{BASE}/4242", json={"name": "x"}).status_code == 404


def test_delete(auth_client: TestClient) -> None:
    dest_id = auth_client.post(BASE, json=YT).json()["id"]
    assert auth_client.delete(f"{BASE}/{dest_id}").status_code == 204
    assert auth_client.get(f"{BASE}/{dest_id}").status_code == 404
    assert auth_client.delete(f"{BASE}/{dest_id}").status_code == 404


def test_mutations_publish_change_events(
    auth_client: TestClient, redis_sync: fakeredis.FakeRedis
) -> None:
    sub = redis_sync.pubsub()
    sub.subscribe("omni:events")
    sub.get_message(timeout=0.1)
    dest_id = auth_client.post(BASE, json=YT).json()["id"]
    auth_client.patch(f"{BASE}/{dest_id}", json={"enabled": False})
    auth_client.delete(f"{BASE}/{dest_id}")
    ops = []
    while (msg := sub.get_message(timeout=0.2)) is not None:
        if msg["type"] == "message":
            data = json.loads(msg["data"])
            assert data["type"] == "destinations.changed"
            ops.append(data["op"])
    assert ops == ["create", "update", "delete"]


def test_runtime_status_from_redis_is_merged(
    auth_client: TestClient, redis_sync: fakeredis.FakeRedis
) -> None:
    dest_id = auth_client.post(BASE, json=YT).json()["id"]
    blob = {
        "id": dest_id,
        "name": "YouTube main",
        "status": "live",
        "last_error": None,
        "pid": 4242,
        "started_at": "2026-10-25T12:00:00+00:00",
        "uptime_s": 12.5,
        "bitrate_kbps": 2501.3,
        "bytes_sent": 1000000,
        "restarts": 1,
        "consecutive_failures": 0,
        "next_retry_at": None,
        "updated_at": "2026-10-25T12:00:12+00:00",
    }
    redis_sync.set(dest_status_key(dest_id), json.dumps(blob))
    d = auth_client.get(f"{BASE}/{dest_id}").json()
    assert d["status"] == "live"
    assert d["runtime"]["pid"] == 4242
    assert d["runtime"]["bitrate_kbps"] == 2501.3
    # disabled rows report disabled regardless of a stale runtime blob
    auth_client.patch(f"{BASE}/{dest_id}", json={"enabled": False})
    assert auth_client.get(f"{BASE}/{dest_id}").json()["status"] == "disabled"
    # error details surface
    redis_sync.set(
        dest_status_key(dest_id), json.dumps({**blob, "status": "retrying", "last_error": "boom"})
    )
    auth_client.patch(f"{BASE}/{dest_id}", json={"enabled": True})
    d = auth_client.get(f"{BASE}/{dest_id}").json()
    assert d["status"] == "retrying"
    assert d["last_error"] == "boom"


# ---- relay router -----------------------------------------------------------------


def test_relay_state_and_switch(auth_client: TestClient, redis_sync: fakeredis.FakeRedis) -> None:
    auth_client.post(BASE, json=YT)
    state = auth_client.get("/api/v1/relay").json()
    assert state["enabled"] is True
    assert state["relay_alive"] is False
    assert state["heartbeat_age_s"] is None
    assert state["ingest"] is None
    assert state["enabled_count"] == 1
    assert state["live_count"] == 0

    assert auth_client.post("/api/v1/relay/stop").status_code == 200
    assert auth_client.get("/api/v1/relay").json()["enabled"] is False
    assert auth_client.post("/api/v1/relay/start").status_code == 200
    assert auth_client.get("/api/v1/relay").json()["enabled"] is True

    redis_sync.set(KEY_RELAY_HEARTBEAT, str(time.time()))
    redis_sync.set(
        "omni:ingest:state",
        json.dumps(
            {
                "live": True,
                "since": "2026-10-25T12:00:00+00:00",
                "bitrate_kbps": 6000.0,
                "tracks": ["H264", "MPEG-4 Audio"],
                "source": "rtmpConn",
            }
        ),
    )
    redis_sync.set(dest_status_key(1), json.dumps({"id": 1, "name": "x", "status": "live"}))
    state = auth_client.get("/api/v1/relay").json()
    assert state["relay_alive"] is True
    assert state["ingest"]["live"] is True
    assert state["ingest"]["tracks"] == ["H264", "MPEG-4 Audio"]
    assert state["live_count"] == 1


def test_ingest_info(auth_client: TestClient) -> None:
    info = auth_client.get("/api/v1/relay/ingest").json()
    assert info["rtmp_server"] == "rtmp://localhost:1935/live"
    assert info["stream_key"] == "test-ingest-key-0123456789"
    assert info["path"] == "live/test-ingest-key-0123456789"
    assert info["srt_url"].startswith("srt://localhost:8890?streamid=publish:live/")
    assert auth_client.get("/api/v1/relay").status_code == 200
