"""Kick chat via the official public API (docs.kick.com, verified 2026-10-25).

Reading: Kick delivers ``chat.message.sent`` events to the app's **webhook URL**
(configured in the Kick developer dashboard as ``https://<domain>/api/v1/webhooks/kick``).
The API process verifies the RSA-SHA256 signature (``Kick-Event-Signature`` over
``message_id.timestamp.body`` with the key from ``GET /public/v1/public-key``) and
re-publishes the payload on Redis; this adapter subscribes to that channel and keeps
only events for its own broadcaster. Subscriptions are created with
``POST /public/v1/events/subscriptions`` (scope ``events:subscribe``).

Sending: ``POST /public/v1/chat`` (scope ``chat:write``, type ``user``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

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
from core.redis import CHANNEL_WEBHOOKS_KICK

log = get_logger(__name__)

EMOTE_RE = re.compile(r"\[emote:(\d+):([^\]]+)\]")


def normalize_payload(
    body: dict[str, Any], account_id: int | None, own_user_id: str
) -> ChatMessage:
    sender = body.get("sender") or {}
    identity = sender.get("identity") or {}
    badges = [str(b.get("type")) for b in identity.get("badges") or [] if b.get("type")]
    sender_id = str(sender.get("user_id", ""))
    text = EMOTE_RE.sub(lambda m: f":{m.group(2)}:", str(body.get("content", "")))
    return ChatMessage(
        id=str(body.get("message_id", "")),
        platform="kick",
        author=str(sender.get("username", "")),
        author_id=sender_id,
        text=text,
        timestamp=parse_timestamp(body.get("created_at")),
        is_own=bool(own_user_id) and sender_id == own_user_id,
        kind="text",
        badges=badges,
        raw=body,
        account_id=account_id,
    )


class KickAdapter:
    platform = "kick"

    def __init__(self, ctx: AdapterContext, *, api_url: str = oauth.KICK_API) -> None:
        self.ctx = ctx
        self.api_url = api_url.rstrip("/")
        self.user_id = ctx.account.external_id
        self.broadcaster_user_id = ""
        self._stop = False
        self._log = log.bind(platform="kick", account_id=ctx.account.id)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ctx.credentials.access_token}",
            "Accept": "application/json",
        }

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        creds, changed = await oauth.ensure_fresh(
            self.ctx.http, self.ctx.settings, "kick", self.ctx.credentials
        )
        if changed:
            self.ctx.credentials = creds
            await self.ctx.persist_credentials()
        try:
            resp = await self.ctx.http.request(
                method, f"{self.api_url}{path}", headers=self._headers(), timeout=15, **kw
            )
        except httpx.HTTPError as exc:
            msg = f"kick api unreachable: {exc}"
            raise AdapterError(msg) from exc
        if resp.status_code == 401 and self.ctx.credentials.refresh_token:
            self.ctx.credentials = await oauth.refresh(
                self.ctx.http, self.ctx.settings, "kick", self.ctx.credentials
            )
            await self.ctx.persist_credentials()
            resp = await self.ctx.http.request(
                method, f"{self.api_url}{path}", headers=self._headers(), timeout=15, **kw
            )
        if resp.status_code in (401, 403):
            msg = f"kick api refused ({resp.status_code}): {resp.text[:200]}; reconnect the account"
            raise AuthError(msg)
        if resp.status_code >= 400:
            msg = f"kick api error {resp.status_code}: {resp.text[:200]}"
            raise AdapterError(msg)
        return resp

    async def connect(self) -> None:
        if not self.ctx.credentials.access_token:
            msg = "kick account has no access token"
            raise AuthError(msg)
        if self.ctx.redis is None:
            msg = "kick adapter needs Redis (webhook relay)"
            raise ConfigError(msg)
        users = (await self._request("GET", "/users")).json().get("data") or []
        if not users:
            msg = "kick: cannot read current user"
            raise AuthError(msg)
        self.user_id = str(users[0].get("user_id", ""))
        channels = (await self._request("GET", "/channels")).json().get("data") or []
        slug = str(channels[0].get("slug", "")) if channels else self.ctx.account.channel_ref
        self.broadcaster_user_id = (
            str(channels[0].get("broadcaster_user_id", self.user_id)) if channels else self.user_id
        )
        if self.ctx.account.external_id != self.user_id or not self.ctx.account.channel_ref:
            await self.ctx.persist_identity(self.user_id, str(users[0].get("name") or slug), slug)
        await self._ensure_subscription()

    async def _ensure_subscription(self) -> None:
        existing = (await self._request("GET", "/events/subscriptions")).json().get("data") or []
        for sub in existing:
            if sub.get("event") == "chat.message.sent" and str(
                sub.get("broadcaster_user_id", "")
            ) in ("", self.broadcaster_user_id):
                return
        body = {"events": [{"name": "chat.message.sent", "version": 1}], "method": "webhook"}
        resp = await self._request("POST", "/events/subscriptions", json=body)
        for item in resp.json().get("data") or []:
            if item.get("error"):
                msg = f"kick subscription error: {item['error']}"
                raise AdapterError(msg)
        self._log.info("kick.subscribed", broadcaster_user_id=self.broadcaster_user_id)

    async def messages(self) -> AsyncIterator[ChatMessage]:
        assert self.ctx.redis is not None
        pubsub = self.ctx.redis.pubsub()
        await pubsub.subscribe(CHANNEL_WEBHOOKS_KICK)
        last_check = time.monotonic()
        try:
            while not self._stop:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None:
                    if time.monotonic() - last_check > 3600:
                        # hourly: keep the token fresh and the subscription alive
                        await self._ensure_subscription()
                        last_check = time.monotonic()
                    await asyncio.sleep(0)
                    continue
                try:
                    envelope = json.loads(message["data"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(envelope, dict) or envelope.get("type") != "chat.message.sent":
                    continue
                body = envelope.get("body") or {}
                broadcaster = (body.get("broadcaster") or {}).get("user_id")
                if str(broadcaster) != self.broadcaster_user_id:
                    continue
                yield normalize_payload(body, self.ctx.account.id, self.user_id)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(CHANNEL_WEBHOOKS_KICK)
                await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def send(self, text: str) -> SendResult:
        if not self.broadcaster_user_id:
            try:
                await self.connect()
            except AdapterError as exc:
                return SendResult(False, error=str(exc))
        body = {
            "type": "user",
            "broadcaster_user_id": int(self.broadcaster_user_id),
            "content": text[:500],
        }
        try:
            resp = await self._request("POST", "/chat", json=body)
        except AdapterError as exc:
            return SendResult(False, error=str(exc))
        data = resp.json().get("data") or {}
        if not data.get("is_sent"):
            return SendResult(False, error="kick did not accept the message")
        return SendResult(True, message_id=str(data.get("message_id", "")))

    async def close(self) -> None:
        self._stop = True
