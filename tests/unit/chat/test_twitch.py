"""Twitch adapter against a local EventSub-like websocket server + mocked Helix."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from websockets.asyncio.server import Server, ServerConnection, serve

from chat import oauth
from chat.adapters.twitch import TwitchAdapter, normalize_event
from chat.base import AdapterError, AuthError
from core.settings import Settings
from tests.unit.chat.conftest import SavedState, load_fixture, make_ctx

NOTIFICATION = load_fixture("twitch_notification.json")


def test_normalize_event() -> None:
    event = dict(NOTIFICATION["payload"]["event"])
    event["_received_at"] = NOTIFICATION["metadata"]["message_timestamp"]
    msg = normalize_event(event, 7, own_user_id="1971641")
    assert msg.id == "cc106a89-1814-919d-454c-f4f2f970aae7"
    assert msg.platform == "twitch"
    assert msg.author == "viewer32"
    assert msg.author_id == "4145994"
    assert msg.text == "Hi chat Kappa"
    assert msg.badges == ["moderator", "subscriber"]
    assert msg.is_own is False
    assert msg.timestamp.year == 2026
    assert msg.key == "twitch:cc106a89-1814-919d-454c-f4f2f970aae7"
    own = normalize_event({**event, "chatter_user_id": "1971641"}, 7, own_user_id="1971641")
    assert own.is_own is True


class FakeEventSub:
    """Minimal EventSub websocket: welcome, then scripted messages after subscribe."""

    def __init__(self) -> None:
        self.server: Server | None = None
        self.subscribed = asyncio.Event()
        self.script: list[dict[str, Any]] = []
        self.connections = 0
        self.url = ""

    async def __aenter__(self) -> FakeEventSub:
        self.server = await serve(self._handler, "127.0.0.1", 0)
        port = next(iter(self.server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/ws"
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        session_id = f"sess-{self.connections}"
        await ws.send(
            json.dumps(
                {
                    "metadata": {"message_type": "session_welcome"},
                    "payload": {
                        "session": {
                            "id": session_id,
                            "keepalive_timeout_seconds": 2,
                            "reconnect_url": None,
                        }
                    },
                }
            )
        )
        closed = asyncio.ensure_future(ws.wait_closed())
        subscribed = asyncio.ensure_future(self.subscribed.wait())
        done, _ = await asyncio.wait(
            {closed, subscribed}, timeout=5, return_when=asyncio.FIRST_COMPLETED
        )
        for t in (closed, subscribed):
            if not t.done():
                t.cancel()
        if subscribed not in done:
            with contextlib.suppress(Exception):
                await ws.close(4003)
            return
        for msg in list(self.script):
            if msg.get("_close"):
                await ws.close(int(msg["_close"]))
                return
            if msg.get("_sleep"):
                # stay silent for a while, but return as soon as the client hangs up
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(ws.wait_closed(), timeout=float(msg["_sleep"]))
                continue
            await ws.send(json.dumps(msg))
        await ws.wait_closed()


def helix_handler(
    fake: FakeEventSub,
    *,
    validate_status: int = 200,
    scopes: list[str] | None = None,
    sub_status: int = 202,
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(oauth.TWITCH_VALIDATE):
            if validate_status != 200:
                return httpx.Response(
                    validate_status, json={"status": 401, "message": "invalid access token"}
                )
            return httpx.Response(
                200,
                json={
                    "client_id": "twitch-client",
                    "login": "streamer25",
                    "user_id": "1971641",
                    "scopes": scopes or ["user:read:chat", "user:write:chat"],
                    "expires_in": 999,
                },
            )
        if url.startswith(oauth.TWITCH_HELIX + "/eventsub/subscriptions"):
            body = json.loads(request.content)
            assert body["type"] == "channel.chat.message"
            assert body["version"] == "1"
            assert body["transport"]["method"] == "websocket"
            assert body["transport"]["session_id"].startswith("sess-")
            assert body["condition"] == {"broadcaster_user_id": "1971641", "user_id": "1971641"}
            assert request.headers["Client-Id"] == "twitch-client"
            if sub_status != 202:
                return httpx.Response(
                    sub_status,
                    json={
                        "error": "Forbidden",
                        "message": "subscription missing proper authorization",
                    },
                )
            fake.subscribed.set()
            return httpx.Response(
                202,
                json={
                    "data": [{"id": "sub1", "status": "enabled"}],
                    "total": 1,
                    "total_cost": 0,
                    "max_total_cost": 10,
                },
            )
        if url.startswith(oauth.TWITCH_HELIX + "/chat/messages"):
            body = json.loads(request.content)
            assert body["broadcaster_id"] == "1971641"
            assert body["sender_id"] == "1971641"
            return httpx.Response(
                200, json={"data": [{"message_id": "sent-1", "is_sent": True, "drop_reason": None}]}
            )
        if url.startswith(oauth.TWITCH_TOKEN):
            return httpx.Response(
                200, json={"access_token": "fresh", "refresh_token": "ref2", "expires_in": 3600}
            )
        return httpx.Response(404)

    return handler


async def collect(adapter: TwitchAdapter, n: int, within: float = 5.0) -> list[Any]:
    out: list[Any] = []

    async def run() -> None:
        async for m in adapter.messages():
            out.append(m)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout=within)
    return out


async def test_receives_messages_and_sends(chat_settings: Settings, saved: SavedState) -> None:
    async with FakeEventSub() as fake:
        fake.script = [
            {"metadata": {"message_type": "session_keepalive"}, "payload": {}},
            NOTIFICATION,
            NOTIFICATION,
        ]
        ctx = make_ctx(chat_settings, "twitch", helix_handler(fake), saved=saved)
        adapter = TwitchAdapter(ctx, ws_url=fake.url)
        await adapter.connect()
        assert adapter.user_id == "1971641"
        assert adapter.broadcaster_id == "1971641"
        assert saved.identity == [("1971641", "Streamer", "")]
        msgs = await collect(adapter, 2)
        assert [m.text for m in msgs] == ["Hi chat Kappa", "Hi chat Kappa"]
        result = await adapter.send("hello")
        assert result.ok
        assert result.message_id == "sent-1"
        await adapter.close()


async def test_reconnect_message_switches_connection(chat_settings: Settings) -> None:
    async with FakeEventSub() as fake:
        reconnect = {
            "metadata": {"message_type": "session_reconnect"},
            "payload": {"session": {"id": "sess-1", "reconnect_url": None}},
        }
        fake.script = [NOTIFICATION, reconnect, NOTIFICATION]
        ctx = make_ctx(chat_settings, "twitch", helix_handler(fake))
        adapter = TwitchAdapter(ctx, ws_url=fake.url)
        await adapter.connect()
        reconnect["payload"]["session"]["reconnect_url"] = fake.url  # type: ignore[index]
        msgs = await collect(adapter, 2, within=8)
        assert len(msgs) == 2
        assert fake.connections == 2
        await adapter.close()


async def test_keepalive_timeout_raises_adapter_error(
    chat_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chat.adapters import twitch as twitch_mod

    monkeypatch.setattr(twitch_mod, "KEEPALIVE_GRACE", 0.5)
    async with FakeEventSub() as fake:
        fake.script = [{"_sleep": 20}]
        ctx = make_ctx(chat_settings, "twitch", helix_handler(fake))
        adapter = TwitchAdapter(ctx, ws_url=fake.url)
        await adapter.connect()
        with pytest.raises(AdapterError, match="keepalive"):
            await asyncio.wait_for(collect(adapter, 1, within=10), timeout=10)
        await adapter.close()


async def test_revocation_is_auth_error(chat_settings: Settings) -> None:
    async with FakeEventSub() as fake:
        fake.script = [
            {
                "metadata": {"message_type": "revocation"},
                "payload": {
                    "subscription": {
                        "status": "authorization_revoked",
                        "type": "channel.chat.message",
                    }
                },
            }
        ]
        ctx = make_ctx(chat_settings, "twitch", helix_handler(fake))
        adapter = TwitchAdapter(ctx, ws_url=fake.url)
        await adapter.connect()
        with pytest.raises(AuthError, match="revoked"):
            await collect(adapter, 1)


async def test_invalid_token_refreshes_then_fails_cleanly(
    chat_settings: Settings, saved: SavedState
) -> None:
    async with FakeEventSub() as fake:
        ctx = make_ctx(
            chat_settings, "twitch", helix_handler(fake, validate_status=401), saved=saved
        )
        adapter = TwitchAdapter(ctx, ws_url=fake.url)
        with pytest.raises(AuthError, match="rejected"):
            await adapter.connect()
        assert saved.credentials
        assert saved.credentials[0].access_token == "fresh"


async def test_missing_scope_and_forbidden_subscription(chat_settings: Settings) -> None:
    async with FakeEventSub() as fake:
        ctx = make_ctx(chat_settings, "twitch", helix_handler(fake, scopes=["user:write:chat"]))
        with pytest.raises(AuthError, match="user:read:chat"):
            await TwitchAdapter(ctx, ws_url=fake.url).connect()
    async with FakeEventSub() as fake:
        ctx = make_ctx(chat_settings, "twitch", helix_handler(fake, sub_status=403))
        adapter = TwitchAdapter(ctx, ws_url=fake.url)
        await adapter.connect()
        with pytest.raises(AuthError, match="refused"):
            await collect(adapter, 1)
