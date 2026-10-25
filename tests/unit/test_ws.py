from __future__ import annotations

import json

import fakeredis
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.redis import CHANNEL_EVENTS, dest_status_key

YT = {"name": "YT", "platform": "youtube", "rtmp_url": "rtmp://a/b", "stream_key": "k"}


def test_ws_requires_session(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info, client.websocket_connect("/ws/events"):
        pass
    assert exc_info.value.code == 4401


def test_ws_snapshot_then_events(auth_client: TestClient, redis_sync: fakeredis.FakeRedis) -> None:
    dest_id = auth_client.post("/api/v1/destinations", json=YT).json()["id"]
    with auth_client.websocket_connect("/ws/events") as ws:
        snap = json.loads(ws.receive_text())
        assert snap["type"] == "snapshot"
        assert [d["id"] for d in snap["destinations"]] == [dest_id]
        assert snap["relay"]["enabled"] is True

        ws.send_text("ping")
        assert ws.receive_text() == "pong"

        # a relay status event is forwarded verbatim
        redis_sync.set(dest_status_key(dest_id), json.dumps({"id": dest_id, "status": "live"}))
        event = {
            "type": "destination.status",
            "id": dest_id,
            "status": "live",
            "bitrate_kbps": 2500,
        }
        redis_sync.publish(CHANNEL_EVENTS, json.dumps(event))
        assert json.loads(ws.receive_text()) == event

        # API mutations are seen too (destinations.changed)
        auth_client.patch(f"/api/v1/destinations/{dest_id}", json={"enabled": False})
        evt = json.loads(ws.receive_text())
        assert evt["type"] == "destinations.changed"
        assert evt["op"] == "update"

        ws.send_text("snapshot")
        snap = json.loads(ws.receive_text())
        assert snap["destinations"][0]["status"] == "disabled"
