"""``/ws/events``: live updates for the dashboard.

Auth: the session cookie (same as REST). On connect the client receives a
``snapshot`` (destinations + relay state); afterwards every event the relay or the
API publishes on the Redis ``omni:events`` channel is forwarded as-is. The client
may send ``"ping"`` and gets ``"pong"``. On any Redis trouble the socket is closed
and the client reconnects (and gets a fresh snapshot).
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from api.services.state import destinations_view, relay_view
from core.logging import get_logger
from core.redis import CHANNEL_CHAT, CHANNEL_EVENTS

router = APIRouter(tags=["ws"])
log = get_logger(__name__)

WS_UNAUTHORIZED = 4401


async def _snapshot(ws: WebSocket) -> str:
    app = ws.app
    with Session(app.state.engine) as session:
        destinations = await destinations_view(session, app.state.redis, app.state.secret_box)
        relay = await relay_view(session, app.state.redis)
    return json.dumps(
        {
            "type": "snapshot",
            "destinations": [d.model_dump(mode="json") for d in destinations],
            "relay": relay.model_dump(mode="json"),
        }
    )


@router.websocket("/ws/events")
async def events(ws: WebSocket) -> None:
    settings = ws.app.state.settings
    user = ws.app.state.signer.read(ws.cookies.get(settings.session_cookie_name))
    if user is None or user != settings.admin_username:
        await ws.close(code=WS_UNAUTHORIZED)
        return
    await ws.accept()
    await ws.send_text(await _snapshot(ws))

    pubsub = ws.app.state.redis.pubsub()
    await pubsub.subscribe(CHANNEL_EVENTS, CHANNEL_CHAT)

    async def forward() -> None:
        async for message in pubsub.listen():
            if message.get("type") == "message":
                data = message["data"]
                await ws.send_text(data if isinstance(data, str) else data.decode())

    async def receive() -> None:
        while True:
            text = await ws.receive_text()
            if text == "ping":
                await ws.send_text("pong")
            elif text == "snapshot":
                await ws.send_text(await _snapshot(ws))

    tasks = [asyncio.create_task(forward()), asyncio.create_task(receive())]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                log.warning("ws.closed_with_error", error=repr(exc))
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(CHANNEL_EVENTS, CHANNEL_CHAT)
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await ws.close()
