from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import fakeredis
import fakeredis.aioredis
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from chat import kick_webhook, oauth
from chat.adapters.kick import KickAdapter, normalize_payload
from core.redis import CHANNEL_WEBHOOKS_KICK
from core.settings import Settings
from tests.unit.chat.conftest import SavedState, load_fixture, make_ctx

PAYLOAD = load_fixture("kick_chat_message.json")


@pytest.fixture(scope="module")
def keypair() -> tuple[bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


def test_normalize_payload() -> None:
    msg = normalize_payload(PAYLOAD, 7, own_user_id="1234567")
    assert msg.id == "01JB2X6N9S9K8W7V6R5T4Y3Z2A"
    assert msg.author == "kickfan"
    assert msg.author_id == "7654321"
    assert msg.text == "hello :KEKW: everyone"
    assert msg.badges == ["moderator", "subscriber"]
    assert msg.is_own is False
    assert msg.timestamp.isoformat().startswith("2026-10-25T19:22:40")


def test_signature_roundtrip(keypair: tuple[bytes, str]) -> None:
    priv, pub = keypair
    body = json.dumps(PAYLOAD).encode()
    sig = kick_webhook.sign_payload(priv, "01JB", "2026-10-25T19:22:41Z", body)
    assert kick_webhook.verify_signature(pub, "01JB", "2026-10-25T19:22:41Z", body, sig)
    assert not kick_webhook.verify_signature(pub, "01JC", "2026-10-25T19:22:41Z", body, sig)
    assert not kick_webhook.verify_signature(pub, "01JB", "2026-10-25T19:22:41Z", body + b" ", sig)
    assert not kick_webhook.verify_signature(
        pub, "01JB", "2026-10-25T19:22:41Z", body, "not base64!"
    )
    assert not kick_webhook.verify_signature("garbage", "01JB", "x", body, sig)


def test_webhook_endpoint_verifies_dedupes_and_publishes(
    client: TestClient, redis_sync: fakeredis.FakeRedis, keypair: tuple[bytes, str]
) -> None:
    priv, pub = keypair
    client.app.state.kick_public_key = kick_webhook.PublicKeyCache(pinned=pub)  # type: ignore[attr-defined]
    sub = redis_sync.pubsub()
    sub.subscribe(CHANNEL_WEBHOOKS_KICK)
    sub.get_message(timeout=0.1)
    body = json.dumps(PAYLOAD).encode()
    headers = {
        kick_webhook.HEADER_MESSAGE_ID: "01JB2X6N9S9K8W7V6R5T4Y3Z2A",
        kick_webhook.HEADER_TIMESTAMP: "2026-10-25T19:22:41Z",
        kick_webhook.HEADER_TYPE: "chat.message.sent",
        kick_webhook.HEADER_VERSION: "1",
        kick_webhook.HEADER_SIGNATURE: kick_webhook.sign_payload(
            priv, "01JB2X6N9S9K8W7V6R5T4Y3Z2A", "2026-10-25T19:22:41Z", body
        ),
        "Content-Type": "application/json",
    }
    assert client.post("/api/v1/webhooks/kick", content=body, headers=headers).status_code == 200
    env = json.loads(sub.get_message(timeout=0.5)["data"])
    assert env["type"] == "chat.message.sent"
    assert env["body"]["message_id"] == PAYLOAD["message_id"]
    # duplicate delivery: accepted but not re-published
    r = client.post("/api/v1/webhooks/kick", content=body, headers=headers)
    assert r.status_code == 200
    assert r.json().get("duplicate") is True
    assert sub.get_message(timeout=0.2) is None
    # tampered body / missing headers
    bad = client.post("/api/v1/webhooks/kick", content=body + b"x", headers=headers)
    assert bad.status_code == 401
    assert client.post("/api/v1/webhooks/kick", content=body).status_code == 400


async def test_public_key_cache_fetches_and_rotates() -> None:
    pems = iter(
        [
            "-----BEGIN PUBLIC KEY-----\nA\n-----END PUBLIC KEY-----",
            "-----BEGIN PUBLIC KEY-----\nB\n-----END PUBLIC KEY-----",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"public_key": next(pems)}, "message": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        cache = kick_webhook.PublicKeyCache(url="https://api.kick.com/public/v1/public-key")
        first = await cache.get(http)
        assert first
        assert "\nA\n" in first
        assert await cache.get(http) == first  # cached
        rotated = await cache.get(http, force=True)
        assert rotated
        assert "\nB\n" in rotated


def kick_handler(subs: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/users"):
            return httpx.Response(200, json={"data": [{"user_id": 1234567, "name": "streamer25"}]})
        if path.endswith("/channels"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "broadcaster_user_id": 1234567,
                            "slug": "streamer25",
                            "stream": {"is_live": True},
                        }
                    ]
                },
            )
        if path.endswith("/events/subscriptions") and request.method == "GET":
            return httpx.Response(200, json={"data": subs})
        if path.endswith("/events/subscriptions") and request.method == "POST":
            body = json.loads(request.content)
            assert body == {
                "events": [{"name": "chat.message.sent", "version": 1}],
                "method": "webhook",
            }
            subs.append(
                {
                    "id": "s1",
                    "event": "chat.message.sent",
                    "broadcaster_user_id": 1234567,
                    "version": 1,
                }
            )
            return httpx.Response(
                200,
                json={
                    "data": [{"name": "chat.message.sent", "version": 1, "subscription_id": "s1"}]
                },
            )
        if path.endswith("/chat") and request.method == "POST":
            body = json.loads(request.content)
            assert body["type"] == "user"
            assert body["broadcaster_user_id"] == 1234567
            return httpx.Response(200, json={"data": {"is_sent": True, "message_id": "m-sent"}})
        return httpx.Response(404)

    return handler


async def test_adapter_subscribes_and_filters_webhook_events(
    chat_settings: Settings, fake_redis: fakeredis.aioredis.FakeRedis, saved: SavedState
) -> None:
    subs: list[dict[str, Any]] = []
    ctx = make_ctx(chat_settings, "kick", kick_handler(subs), saved=saved, redis=fake_redis)
    adapter = KickAdapter(ctx)
    await adapter.connect()
    assert subs
    assert subs[0]["event"] == "chat.message.sent"
    assert saved.identity == [("1234567", "streamer25", "streamer25")]
    await KickAdapter(ctx).connect()  # existing subscription is reused
    assert len(subs) == 1

    received = []

    async def consume() -> None:
        async for m in adapter.messages():
            received.append(m)
            if len(received) == 1:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    other = {**PAYLOAD, "broadcaster": {**PAYLOAD["broadcaster"], "user_id": 999}}
    await fake_redis.publish(
        CHANNEL_WEBHOOKS_KICK, json.dumps({"type": "chat.message.sent", "body": other})
    )
    await fake_redis.publish(
        CHANNEL_WEBHOOKS_KICK, json.dumps({"type": "channel.followed", "body": PAYLOAD})
    )
    await fake_redis.publish(
        CHANNEL_WEBHOOKS_KICK, json.dumps({"type": "chat.message.sent", "body": PAYLOAD})
    )
    await asyncio.wait_for(task, timeout=5)
    assert len(received) == 1
    assert received[0].author == "kickfan"
    res = await adapter.send("yo")
    assert res.ok
    assert res.message_id == "m-sent"
    await adapter.close()


async def test_adapter_auth_errors(
    chat_settings: Settings, fake_redis: fakeredis.aioredis.FakeRedis
) -> None:
    from chat.base import AuthError

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(oauth.KICK_TOKEN):
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(401, json={"message": "unauthorized"})

    ctx = make_ctx(chat_settings, "kick", handler, redis=fake_redis)
    with pytest.raises(AuthError):
        await KickAdapter(ctx).connect()
