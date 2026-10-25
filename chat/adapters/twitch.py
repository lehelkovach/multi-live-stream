"""Twitch chat via EventSub over WebSocket (``channel.chat.message`` v1) + Helix send.

Flow (dev.twitch.tv/docs/eventsub/handling-websocket-events, verified 2026-10-25):
connect ``wss://eventsub.wss.twitch.tv/ws`` -> ``session_welcome`` (session id) ->
POST ``/helix/eventsub/subscriptions`` with ``transport {method: websocket, session_id}``
within 10 s -> ``notification`` messages. ``session_keepalive`` resets the watchdog,
``session_reconnect`` carries a ``reconnect_url`` we must switch to, ``revocation``
means our authorization is gone.

Token: the broadcaster's user token with ``user:read:chat`` (``user:write:chat`` to
send). Validated hourly as Twitch requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection, connect

from chat import oauth
from chat.base import (
    AdapterContext,
    AdapterError,
    AuthError,
    ChatMessage,
    ConfigError,
    SendResult,
    parse_timestamp,
)
from core.logging import get_logger

log = get_logger(__name__)

EVENTSUB_WS = "wss://eventsub.wss.twitch.tv/ws"
KEEPALIVE_SECONDS = 30
KEEPALIVE_GRACE = 10.0  # extra seconds past the keepalive timeout before we reconnect
VALIDATE_EVERY = 3600.0


def normalize_event(event: dict[str, Any], account_id: int | None, own_user_id: str) -> ChatMessage:
    """``payload.event`` of a channel.chat.message notification -> ChatMessage."""
    badges = []
    for badge in event.get("badges") or []:
        set_id = str(badge.get("set_id", ""))
        if set_id:
            badges.append(set_id)
    message = event.get("message") or {}
    kind = "text"
    if event.get("cheer"):
        kind = "superchat"
    chatter_id = str(event.get("chatter_user_id", ""))
    return ChatMessage(
        id=str(event.get("message_id", "")),
        platform="twitch",
        author=str(event.get("chatter_user_name") or event.get("chatter_user_login") or ""),
        author_id=chatter_id,
        text=str(message.get("text", "")),
        timestamp=parse_timestamp(event.get("_received_at")),
        is_own=bool(own_user_id) and chatter_id == own_user_id,
        kind=kind,
        badges=badges,
        raw=event,
        account_id=account_id,
    )


class TwitchAdapter:
    platform = "twitch"

    def __init__(
        self, ctx: AdapterContext, *, ws_url: str = EVENTSUB_WS, helix_url: str = oauth.TWITCH_HELIX
    ) -> None:
        self.ctx = ctx
        self.ws_url = ws_url
        self.helix_url = helix_url
        self.user_id = ctx.account.external_id
        self.broadcaster_id = ""
        self._client_id = ""
        self._last_validated = 0.0
        self._ws: ClientConnection | None = None
        self._log = log.bind(platform="twitch", account_id=ctx.account.id)

    # ---- helpers -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ctx.credentials.access_token}",
            "Client-Id": self._client_id,
            "Content-Type": "application/json",
        }

    async def _validate(self) -> None:
        """Validate (hourly, as required) and refresh when expiring."""
        creds, changed = await oauth.ensure_fresh(
            self.ctx.http, self.ctx.settings, "twitch", self.ctx.credentials
        )
        if changed:
            self.ctx.credentials = creds
            await self.ctx.persist_credentials()
        resp = await self.ctx.http.get(
            oauth.TWITCH_VALIDATE,
            headers={"Authorization": f"OAuth {creds.access_token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            if creds.refresh_token:
                self.ctx.credentials = await oauth.refresh(
                    self.ctx.http, self.ctx.settings, "twitch", creds
                )
                await self.ctx.persist_credentials()
                resp = await self.ctx.http.get(
                    oauth.TWITCH_VALIDATE,
                    headers={"Authorization": f"OAuth {self.ctx.credentials.access_token}"},
                    timeout=15,
                )
            if resp.status_code == 401:
                msg = "twitch token rejected; reconnect the account"
                raise AuthError(msg)
        if resp.status_code != 200:
            msg = f"twitch validate failed ({resp.status_code})"
            raise AdapterError(msg)
        data = resp.json()
        self._client_id = str(data.get("client_id") or self.ctx.settings.twitch_client_id)
        if not self.user_id:
            self.user_id = str(data.get("user_id", ""))
        if "user:read:chat" not in (data.get("scopes") or []):
            msg = "twitch token lacks user:read:chat; reconnect the account"
            raise AuthError(msg)
        self._last_validated = time.monotonic()

    async def _resolve_broadcaster(self) -> None:
        login = self.ctx.account.channel_ref.strip().lstrip("#").lower()
        if not login or login == self.ctx.account.display_name.lower():
            self.broadcaster_id = self.user_id
            return
        resp = await self.ctx.http.get(
            f"{self.helix_url}/users", params={"login": login}, headers=self._headers(), timeout=15
        )
        if resp.status_code != 200 or not resp.json().get("data"):
            msg = f"twitch channel {login!r} not found"
            raise ConfigError(msg)
        self.broadcaster_id = str(resp.json()["data"][0]["id"])

    async def _subscribe(self, session_id: str) -> None:
        body = {
            "type": "channel.chat.message",
            "version": "1",
            "condition": {"broadcaster_user_id": self.broadcaster_id, "user_id": self.user_id},
            "transport": {"method": "websocket", "session_id": session_id},
        }
        resp = await self.ctx.http.post(
            f"{self.helix_url}/eventsub/subscriptions",
            json=body,
            headers=self._headers(),
            timeout=15,
        )
        if resp.status_code in (401, 403):
            msg = f"twitch eventsub subscription refused ({resp.status_code}): {resp.text[:200]}"
            raise AuthError(msg)
        if resp.status_code >= 400:
            msg = f"twitch eventsub subscription failed ({resp.status_code}): {resp.text[:200]}"
            raise AdapterError(msg)
        self._log.info("twitch.subscribed", broadcaster_id=self.broadcaster_id)

    # ---- protocol ------------------------------------------------------------

    async def connect(self) -> None:
        if not self.ctx.credentials.access_token:
            msg = "twitch account has no access token"
            raise AuthError(msg)
        await self._validate()
        if not self.user_id:
            msg = "twitch token has no user id (app token?)"
            raise AuthError(msg)
        await self._resolve_broadcaster()
        if self.ctx.account.external_id != self.user_id:
            await self.ctx.persist_identity(
                self.user_id, self.ctx.account.display_name, self.ctx.account.channel_ref
            )

    async def messages(self) -> AsyncIterator[ChatMessage]:
        url = f"{self.ws_url}?keepalive_timeout_seconds={KEEPALIVE_SECONDS}"
        ws = await self._open(url)
        try:
            keepalive = KEEPALIVE_SECONDS
            welcome = await self._recv(ws, within=15)
            if welcome.get("metadata", {}).get("message_type") != "session_welcome":
                msg = "twitch: expected session_welcome"
                raise AdapterError(msg)
            session = welcome["payload"]["session"]
            keepalive = int(session.get("keepalive_timeout_seconds") or keepalive)
            await self._subscribe(str(session["id"]))
            while True:
                if time.monotonic() - self._last_validated > VALIDATE_EVERY:
                    await self._validate()
                frame = await self._recv(ws, within=keepalive + KEEPALIVE_GRACE)
                mtype = frame.get("metadata", {}).get("message_type")
                if mtype == "notification":
                    payload = frame.get("payload", {})
                    if payload.get("subscription", {}).get("type") != "channel.chat.message":
                        continue
                    event = dict(payload.get("event") or {})
                    event["_received_at"] = frame.get("metadata", {}).get("message_timestamp")
                    yield normalize_event(event, self.ctx.account.id, self.user_id)
                elif mtype == "session_keepalive":
                    continue
                elif mtype == "session_reconnect":
                    new_url = frame["payload"]["session"]["reconnect_url"]
                    self._log.info("twitch.reconnect_requested")
                    new_ws = await self._open(new_url)
                    welcome = await self._recv(new_ws, within=15)
                    if welcome.get("metadata", {}).get("message_type") != "session_welcome":
                        await new_ws.close()
                        msg_ = "twitch: reconnect without welcome"
                        raise AdapterError(msg_)
                    await ws.close()
                    ws = new_ws
                    self._ws = ws
                elif mtype == "revocation":
                    status = frame.get("payload", {}).get("subscription", {}).get("status", "")
                    msg_ = f"twitch subscription revoked ({status})"
                    raise AuthError(msg_)
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
            self._ws = None

    async def _open(self, url: str) -> ClientConnection:
        try:
            ws = await connect(url, open_timeout=15, ping_interval=None, max_size=2**20)
        except (TimeoutError, OSError, websockets.WebSocketException) as exc:
            msg = f"twitch eventsub connect failed: {exc}"
            raise AdapterError(msg) from exc
        self._ws = ws
        return ws

    async def _recv(self, ws: ClientConnection, *, within: float) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=within)
        except TimeoutError as exc:
            msg = "twitch eventsub: keepalive timeout"
            raise AdapterError(msg) from exc
        except websockets.ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            msg = f"twitch eventsub closed ({code})"
            raise AdapterError(msg) from exc
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    async def send(self, text: str) -> SendResult:
        if not self._client_id:
            await self._validate()
        if not self.broadcaster_id:
            await self._resolve_broadcaster()
        body = {
            "broadcaster_id": self.broadcaster_id,
            "sender_id": self.user_id,
            "message": text[:500],
        }
        try:
            resp = await self.ctx.http.post(
                f"{self.helix_url}/chat/messages", json=body, headers=self._headers(), timeout=15
            )
        except httpx.HTTPError as exc:
            return SendResult(False, error=str(exc))
        if resp.status_code >= 400:
            return SendResult(False, error=f"{resp.status_code}: {resp.text[:200]}")
        data = (resp.json().get("data") or [{}])[0]
        if not data.get("is_sent"):
            reason = data.get("drop_reason") or {}
            return SendResult(
                False, error=str(reason.get("message") or reason.get("code") or "dropped")
            )
        return SendResult(True, message_id=str(data.get("message_id", "")))

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
