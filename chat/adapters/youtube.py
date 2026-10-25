"""YouTube Live Chat via the Data API v3 (verified 2026-10-25).

* Find the chat: ``liveBroadcasts.list?broadcastStatus=active`` (1 unit) ->
  ``snippet.liveChatId``; falls back to ``extra.live_chat_id`` if the owner set one.
* Read: ``liveChatMessages.list`` (5 units/call). We honour ``pollingIntervalMillis``
  and stretch the interval further when chat is quiet and when the daily quota
  budget (``OMNI_YOUTUBE_DAILY_QUOTA * OMNI_YOUTUBE_CHAT_QUOTA_SHARE``) is running
  low, so a long stream never burns through the project's quota.
* Experimental: ``liveChatMessages.streamList`` (``OMNI_YOUTUBE_STREAM_MODE``), one
  5-unit server-streaming call instead of polling; any error falls back to polling.
* Send: ``liveChatMessages.insert`` (20 units).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from chat import oauth
from chat.base import (
    AdapterContext,
    AdapterError,
    AuthError,
    ChatMessage,
    NotLiveError,
    SendResult,
    parse_timestamp,
)
from core.logging import get_logger
from core.redis import KEY_YOUTUBE_QUOTA

log = get_logger(__name__)

COST_LIST = 5
COST_STREAM = 5
COST_INSERT = 20
COST_BROADCASTS = 1
MIN_INTERVAL = 3.0
MAX_INTERVAL = 30.0
IDLE_AFTER = 30.0  # seconds without messages before slowing down
TEXT_TYPES = {
    "textMessageEvent",
    "superChatEvent",
    "superStickerEvent",
    "memberMilestoneChatEvent",
    "newSponsorEvent",
}


def normalize_item(
    item: dict[str, Any], account_id: int | None, own_channel_id: str
) -> ChatMessage | None:
    snippet = item.get("snippet") or {}
    kind_raw = str(snippet.get("type", ""))
    if kind_raw not in TEXT_TYPES:
        return None
    author = item.get("authorDetails") or {}
    details = snippet.get("textMessageDetails") or {}
    text = str(details.get("messageText") or snippet.get("displayMessage") or "")
    badges = [
        name
        for flag, name in (
            ("isChatOwner", "owner"),
            ("isChatModerator", "moderator"),
            ("isChatSponsor", "member"),
            ("isVerified", "verified"),
        )
        if author.get(flag)
    ]
    kind = "superchat" if kind_raw in ("superChatEvent", "superStickerEvent") else "text"
    if kind_raw == "superChatEvent":
        sc = snippet.get("superChatDetails") or {}
        amount = sc.get("amountDisplayString")
        if amount:
            text = f"[{amount}] {sc.get('userComment') or text}".strip()
    channel_id = str(author.get("channelId") or snippet.get("authorChannelId") or "")
    return ChatMessage(
        id=str(item.get("id", "")),
        platform="youtube",
        author=str(author.get("displayName", "")),
        author_id=channel_id,
        text=text,
        timestamp=parse_timestamp(snippet.get("publishedAt")),
        is_own=bool(own_channel_id) and channel_id == own_channel_id,
        kind=kind,
        badges=badges,
        raw=item,
        account_id=account_id,
    )


class QuotaBudget:
    """Tracks units spent today (UTC) in Redis; suggests a polling interval that
    spreads the remaining budget over the rest of the day."""

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self._local = 0

    @staticmethod
    def _day() -> str:
        return datetime.now(UTC).strftime("%Y%m%d")

    async def spend(self, units: int) -> None:
        self._local += units
        if self.ctx.redis is None:
            return
        try:
            key = KEY_YOUTUBE_QUOTA.format(day=self._day())
            await self.ctx.redis.incrby(key, units)
            await self.ctx.redis.expire(key, 2 * 86400)
        except Exception as exc:
            log.debug("youtube.quota_track_failed", error=str(exc))

    async def spent(self) -> int:
        if self.ctx.redis is None:
            return self._local
        try:
            raw = await self.ctx.redis.get(KEY_YOUTUBE_QUOTA.format(day=self._day()))
            return int(raw) if raw else 0
        except Exception:
            return self._local

    async def min_interval(self) -> float:
        """Seconds between list calls so the remaining budget lasts until midnight UTC."""
        budget = int(
            self.ctx.settings.youtube_daily_quota * self.ctx.settings.youtube_chat_quota_share
        )
        remaining = budget - await self.spent()
        if remaining <= 0:
            return MAX_INTERVAL * 10  # 5 minutes: effectively paused until the quota resets
        now = datetime.now(UTC)
        seconds_left = max(60.0, 86400 - (now.hour * 3600 + now.minute * 60 + now.second))
        calls_left = remaining / COST_LIST
        return max(MIN_INTERVAL, seconds_left / max(calls_left, 1.0))


class YouTubeAdapter:
    platform = "youtube"

    def __init__(
        self,
        ctx: AdapterContext,
        *,
        api_url: str = oauth.YOUTUBE_API,
        stream_url: str | None = None,
    ) -> None:
        self.ctx = ctx
        self.api_url = api_url.rstrip("/")
        self.stream_url = (
            stream_url or "https://youtube.googleapis.com/youtube/v3/liveChat/messages/stream"
        )
        self.live_chat_id = ""
        self.quota = QuotaBudget(ctx)
        self._closed = False
        self._log = log.bind(platform="youtube", account_id=ctx.account.id)

    # ---- helpers -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.ctx.credentials.access_token}",
            "Accept": "application/json",
        }

    async def _fresh(self) -> None:
        creds, changed = await oauth.ensure_fresh(
            self.ctx.http, self.ctx.settings, "youtube", self.ctx.credentials
        )
        if changed:
            self.ctx.credentials = creds
            await self.ctx.persist_credentials()

    async def _get(self, path: str, params: dict[str, Any], cost: int) -> dict[str, Any]:
        await self._fresh()
        await self.quota.spend(cost)
        try:
            resp = await self.ctx.http.get(
                f"{self.api_url}/{path}", params=params, headers=self._headers(), timeout=20
            )
        except httpx.HTTPError as exc:
            msg = f"youtube api unreachable: {exc}"
            raise AdapterError(msg) from exc
        if resp.status_code == 401:
            if self.ctx.credentials.refresh_token:
                self.ctx.credentials = await oauth.refresh(
                    self.ctx.http, self.ctx.settings, "youtube", self.ctx.credentials
                )
                await self.ctx.persist_credentials()
                resp = await self.ctx.http.get(
                    f"{self.api_url}/{path}", params=params, headers=self._headers(), timeout=20
                )
            if resp.status_code == 401:
                msg = "youtube token rejected; reconnect the account"
                raise AuthError(msg)
        if resp.status_code == 403:
            reason = ""
            try:
                reason = str(resp.json()["error"]["errors"][0]["reason"])
            except (KeyError, IndexError, TypeError, ValueError):
                reason = resp.text[:200]
            if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                msg = f"youtube quota exhausted ({reason}); resumes after the daily reset"
                raise NotLiveError(msg)
            if reason in ("liveChatEnded", "liveChatDisabled", "forbidden", "liveChatNotFound"):
                msg = f"youtube chat unavailable ({reason})"
                raise NotLiveError(msg)
            msg = f"youtube api forbidden ({reason})"
            raise AuthError(msg)
        if resp.status_code == 404:
            msg = "youtube live chat not found (stream ended?)"
            raise NotLiveError(msg)
        if resp.status_code >= 400:
            msg = f"youtube api error {resp.status_code}: {resp.text[:200]}"
            raise AdapterError(msg)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _find_live_chat(self) -> str:
        override = str(self.ctx.credentials.extra.get("live_chat_id") or "")
        if override:
            return override
        data = await self._get(
            "liveBroadcasts",
            {"part": "snippet,status", "broadcastStatus": "active", "maxResults": 5},
            COST_BROADCASTS,
        )
        for item in data.get("items", []):
            status = (item.get("status") or {}).get("lifeCycleStatus")
            chat_id = (item.get("snippet") or {}).get("liveChatId")
            if chat_id and status in ("live", "liveStarting", "testing", "testStarting", "ready"):
                return str(chat_id)
        msg = "no active YouTube broadcast"
        raise NotLiveError(msg)

    # ---- protocol ------------------------------------------------------------

    async def connect(self) -> None:
        if not self.ctx.credentials.access_token:
            msg = "youtube account has no access token"
            raise AuthError(msg)
        self.live_chat_id = await self._find_live_chat()
        self._log.info("youtube.chat_found", live_chat_id=self.live_chat_id)

    async def messages(self) -> AsyncIterator[ChatMessage]:
        if self.ctx.settings.youtube_stream_mode:
            try:
                async for message in self._stream():
                    yield message
                return
            except (AuthError, NotLiveError):
                raise
            except Exception as exc:
                self._log.warning("youtube.stream_mode_failed", error=str(exc))
        async for message in self._poll():
            yield message

    async def _poll(self) -> AsyncIterator[ChatMessage]:
        page_token: str | None = None
        last_message_at = time.monotonic()
        own = self.ctx.account.external_id
        while not self._closed:
            params: dict[str, Any] = {
                "liveChatId": self.live_chat_id,
                "part": "snippet,authorDetails",
                "maxResults": 500,
            }
            if page_token:
                params["pageToken"] = page_token
            data = await self._get("liveChat/messages", params, COST_LIST)
            items = data.get("items", [])
            if page_token is not None or not items:
                # the first page without a token is history we do not replay... except
                # when it is short (fresh chat); keep behaviour simple: replay first page
                pass
            for item in items:
                message = normalize_item(item, self.ctx.account.id, own)
                if message is not None:
                    yield message
            if items:
                last_message_at = time.monotonic()
            page_token = data.get("nextPageToken") or page_token
            if data.get("offlineAt"):
                msg_ = "youtube chat ended"
                raise NotLiveError(msg_)
            suggested = float(data.get("pollingIntervalMillis") or 5000) / 1000.0
            interval = max(suggested, MIN_INTERVAL, await self.quota.min_interval())
            if time.monotonic() - last_message_at > IDLE_AFTER:
                interval = max(interval, min(MAX_INTERVAL, suggested * 3))
            await asyncio.sleep(min(interval, MAX_INTERVAL * 10))

    async def _stream(self) -> AsyncIterator[ChatMessage]:
        """streamList: one long-lived response carrying many JSON documents."""
        await self._fresh()
        await self.quota.spend(COST_STREAM)
        params = {"liveChatId": self.live_chat_id, "part": "snippet,authorDetails"}
        own = self.ctx.account.external_id
        async with self.ctx.http.stream(
            "GET",
            self.stream_url,
            params=params,
            headers=self._headers(),
            timeout=httpx.Timeout(30, read=None),
        ) as resp:
            if resp.status_code == 401:
                msg = "youtube token rejected; reconnect the account"
                raise AuthError(msg)
            if resp.status_code >= 400:
                body = await resp.aread()
                msg = f"youtube streamList error {resp.status_code}: {body[:200]!r}"
                raise AdapterError(msg)
            decoder = json.JSONDecoder()
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while True:
                    buffer = buffer.lstrip(" \r\n\t,[")
                    if not buffer or buffer[0] == "]":
                        buffer = buffer[1:] if buffer[:1] == "]" else buffer
                        break
                    try:
                        doc, end = decoder.raw_decode(buffer)
                    except ValueError:
                        break  # need more data
                    buffer = buffer[end:]
                    if isinstance(doc, dict):
                        for item in doc.get("items", []):
                            message = normalize_item(item, self.ctx.account.id, own)
                            if message is not None:
                                yield message
                        if doc.get("offlineAt"):
                            msg_ = "youtube chat ended"
                            raise NotLiveError(msg_)

    async def send(self, text: str) -> SendResult:
        if not self.live_chat_id:
            try:
                self.live_chat_id = await self._find_live_chat()
            except AdapterError as exc:
                return SendResult(False, error=str(exc))
        await self._fresh()
        await self.quota.spend(COST_INSERT)
        body = {
            "snippet": {
                "liveChatId": self.live_chat_id,
                "type": "textMessageEvent",
                "textMessageDetails": {"messageText": text[:200]},
            }
        }
        try:
            resp = await self.ctx.http.post(
                f"{self.api_url}/liveChat/messages",
                params={"part": "snippet"},
                json=body,
                headers=self._headers(),
                timeout=20,
            )
        except httpx.HTTPError as exc:
            return SendResult(False, error=str(exc))
        if resp.status_code >= 400:
            return SendResult(False, error=f"{resp.status_code}: {resp.text[:200]}")
        return SendResult(True, message_id=str(resp.json().get("id", "")))

    async def close(self) -> None:
        self._closed = True
