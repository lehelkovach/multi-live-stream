from __future__ import annotations

import json

import fakeredis
from fastapi.testclient import TestClient

from core.redis import CHANNEL_EVENTS
from tests.conftest import INGEST_KEY

HOOK = "/internal/mediamtx/auth"


def body(**kw: str) -> dict[str, str]:
    base = {
        "ip": "93.184.216.34",
        "user": "",
        "password": "",
        "token": "",
        "action": "publish",
        "path": f"live/{INGEST_KEY}",
        "protocol": "rtmp",
        "id": "abc",
        "query": "",
        "userAgent": "OBS",
    }
    base.update(kw)
    return base


def test_publish_with_correct_key_allowed(
    client: TestClient, redis_sync: fakeredis.FakeRedis
) -> None:
    sub = redis_sync.pubsub()
    sub.subscribe(CHANNEL_EVENTS)
    sub.get_message(timeout=0.1)  # subscribe ack
    resp = client.post(HOOK, json=body())
    assert resp.status_code == 200
    msg = sub.get_message(timeout=0.5)
    assert msg is not None
    assert json.loads(msg["data"])["type"] == "ingest.publish"


def test_publish_with_wrong_key_denied(client: TestClient) -> None:
    assert client.post(HOOK, json=body(path="live/wrong")).status_code == 401
    assert client.post(HOOK, json=body(path=f"other/{INGEST_KEY}")).status_code == 401
    assert client.post(HOOK, json=body(path=f"live/{INGEST_KEY}/extra")).status_code == 401
    assert client.post(HOOK, json=body(path="")).status_code == 401


def test_publish_denied_when_no_key_configured(app, client: TestClient) -> None:  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    app.state.settings = app.state.settings.model_copy(update={"ingest_stream_key": SecretStr("")})
    assert client.post(HOOK, json=body(path="live/")).status_code == 401


def test_read_allowed_only_from_internal_addresses(client: TestClient) -> None:
    assert (
        client.post(HOOK, json=body(action="read", ip="172.18.0.5", protocol="rtsp")).status_code
        == 200
    )
    assert client.post(HOOK, json=body(action="read", ip="10.0.0.9")).status_code == 200
    assert client.post(HOOK, json=body(action="read", ip="127.0.0.1")).status_code == 200
    assert client.post(HOOK, json=body(action="read", ip="::1")).status_code == 200
    assert client.post(HOOK, json=body(action="read", ip="93.184.216.34")).status_code == 401
    assert client.post(HOOK, json=body(action="read", ip="not-an-ip")).status_code == 401


def test_other_actions_denied(client: TestClient) -> None:
    assert client.post(HOOK, json=body(action="api")).status_code == 401
    assert client.post(HOOK, json=body(action="")).status_code == 401


def test_hook_does_not_require_dashboard_session(client: TestClient) -> None:
    # Called by MediaMTX from inside the Compose network; the edge blocks /internal/*.
    assert client.post(HOOK, json=body()).status_code == 200


def test_publish_brute_force_is_rate_limited(client: TestClient) -> None:
    for _ in range(30):
        client.post(HOOK, json=body(path="live/guess", ip="8.8.8.7"))
    assert client.post(HOOK, json=body(ip="8.8.8.7")).status_code == 401  # even the right key
    assert client.post(HOOK, json=body(ip="8.8.8.8")).status_code == 200  # other ip fine
