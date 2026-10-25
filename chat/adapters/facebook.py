"""Facebook Live comments via the Graph API (verified 2026-10-25).

* Live video: ``GET /{page-id}/live_videos?broadcast_status=["LIVE"]`` with a Page token.
* Read: Server-Sent Events from ``https://streaming-graph.facebook.com/{live-video-id}/live_comments``
  (``data: {...}`` lines, ``: ping`` keep-alives). If the stream cannot be opened we
  fall back to polling ``GET /{live-video-id}/comments`` (reverse chronological,
  ``live_filter=no_filter``) every few seconds, deduplicating by comment id.
* Send: not available on this edge in the current reference; implemented in Phase 4
  after verification.

Token: a Page access token. After OAuth we store every page's token in
``credentials.extra['pages']``; ``channel_ref`` selects the page. A manually pasted
Page token works too (``access_token`` with ``channel_ref`` = page id).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import httpx

from chat.base import (
    AdapterContext,
    AdapterError,
    AuthError,
    ChatMessage,
    ConfigError,
    NotLiveError,
    SendResult,
    parse_timestamp,
)
from core.logging import get_logger

log = get_logger(__name__)

GRAPH_URL = "https://graph.facebook.com"
STREAM_URL = "https://streaming-graph.facebook.com"
COMMENT_FIELDS = "id,from{name,id},message,created_time"
POLL_INTERVAL = 3.0
LIVE_CHECK_EVERY = 60.0


def normalize_comment(comment: dict[str, Any], account_id: int | None, page_id: str) -> ChatMessage:
    sender = comment.get("from") or {}
    sender_id = str(sender.get("id", ""))
    return ChatMessage(
        id=str(comment.get("id", "")),
        platform="facebook",
        author=str(sender.get("name", "") or "Facebook user"),
        author_id=sender_id,
        text=str(comment.get("message", "")),
        timestamp=parse_timestamp(comment.get("created_time")),
        is_own=bool(page_id) and sender_id == page_id,
        kind="text",
        raw=comment,
        account_id=account_id,
    )


def _graph_error(resp: httpx.Response) -> AdapterError:
    code: Any = None
    message = resp.text[:200]
    try:
        err = resp.json().get("error") or {}
        code = err.get("code")
        message = str(err.get("message") or message)
    except ValueError:
        pass
    if code in (190, 102, 200, 10) or resp.status_code in (401, 403):
        return AuthError(f"facebook: {message} (code {code})")
    return AdapterError(f"facebook: {message} (code {code}, http {resp.status_code})")


class FacebookAdapter:
    platform = "facebook"

    def __init__(
        self, ctx: AdapterContext, *, graph_url: str = GRAPH_URL, stream_url: str = STREAM_URL
    ) -> None:
        self.ctx = ctx
        self.graph_url = graph_url.rstrip("/")
        self.stream_url = stream_url.rstrip("/")
        self.page_id = ctx.account.channel_ref.strip()
        self.live_video_id = ""
        self._token = ""
        self._stop = False
        self._log = log.bind(platform="facebook", account_id=ctx.account.id)

    @property
    def _v(self) -> str:
        return self.ctx.settings.graph_api_version

    def _resolve_token(self) -> str:
        pages = self.ctx.credentials.extra.get("pages") or []
        if not self.page_id:
            if len(pages) == 1:
                self.page_id = str(pages[0]["id"])
            else:
                msg = "facebook: choose which Page to read (channel_ref = page id)"
                raise ConfigError(msg)
        for page in pages:
            if str(page.get("id")) == self.page_id and page.get("access_token"):
                return str(page["access_token"])
        if self.ctx.credentials.access_token:
            return self.ctx.credentials.access_token
        msg = "facebook: no access token for this Page; reconnect the account"
        raise AuthError(msg)

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.ctx.http.get(
                url, params={**params, "access_token": self._token}, timeout=20
            )
        except httpx.HTTPError as exc:
            msg = f"facebook graph unreachable: {exc}"
            raise AdapterError(msg) from exc
        if resp.status_code >= 400:
            raise _graph_error(resp)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _find_live_video(self) -> str:
        override = str(self.ctx.credentials.extra.get("live_video_id") or "")
        if override:
            return override
        data = await self._get(
            f"{self.graph_url}/{self._v}/{self.page_id}/live_videos",
            {"fields": "id,status,title,permalink_url", "broadcast_status": '["LIVE"]', "limit": 5},
        )
        for item in data.get("data", []):
            if item.get("status") in (None, "LIVE") and item.get("id"):
                return str(item["id"])
        msg = "no live Facebook video on this Page"
        raise NotLiveError(msg)

    async def connect(self) -> None:
        self._token = self._resolve_token()
        self.live_video_id = await self._find_live_video()
        self._log.info("facebook.live_video_found", live_video_id=self.live_video_id)
        if not self.ctx.account.external_id:
            await self.ctx.persist_identity(
                f"page:{self.page_id}", self.ctx.account.display_name, self.page_id
            )

    async def messages(self) -> AsyncIterator[ChatMessage]:
        try:
            async for message in self._sse():
                yield message
        except (AuthError, NotLiveError):
            raise
        except AdapterError as exc:
            self._log.warning("facebook.sse_unavailable", error=str(exc))
        async for message in self._poll():
            yield message

    async def _sse(self) -> AsyncIterator[ChatMessage]:
        url = f"{self.stream_url}/{self.live_video_id}/live_comments"
        params = {"access_token": self._token, "fields": COMMENT_FIELDS}
        try:
            async with self.ctx.http.stream(
                "GET", url, params=params, timeout=httpx.Timeout(20, read=None)
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _graph_error(resp)
                async for line in resp.aiter_lines():
                    if self._stop:
                        return
                    if not line.startswith("data:"):
                        continue
                    try:
                        comment = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(comment, dict) and comment.get("id"):
                        yield normalize_comment(comment, self.ctx.account.id, self.page_id)
        except httpx.HTTPError as exc:
            msg = f"facebook live_comments stream failed: {exc}"
            raise AdapterError(msg) from exc
        msg_ = "facebook live_comments stream ended"
        raise NotLiveError(msg_)

    async def _poll(self) -> AsyncIterator[ChatMessage]:
        seen: deque[str] = deque(maxlen=2000)
        seen_set: set[str] = set()
        last_live_check = time.monotonic()
        while not self._stop:
            data = await self._get(
                f"{self.graph_url}/{self._v}/{self.live_video_id}/comments",
                {
                    "fields": COMMENT_FIELDS,
                    "order": "reverse_chronological",
                    "live_filter": "no_filter",
                    "limit": 50,
                },
            )
            fresh = [c for c in data.get("data", []) if c.get("id") and c["id"] not in seen_set]
            for comment in reversed(fresh):
                cid = str(comment["id"])
                if len(seen) == seen.maxlen:
                    seen_set.discard(seen[0])
                seen.append(cid)
                seen_set.add(cid)
                yield normalize_comment(comment, self.ctx.account.id, self.page_id)
            if time.monotonic() - last_live_check > LIVE_CHECK_EVERY:
                status = await self._get(
                    f"{self.graph_url}/{self._v}/{self.live_video_id}", {"fields": "status"}
                )
                if status.get("status") not in (None, "LIVE"):
                    msg = f"facebook live video ended ({status.get('status')})"
                    raise NotLiveError(msg)
                last_live_check = time.monotonic()
            await asyncio.sleep(POLL_INTERVAL)

    async def _video_id(self) -> str:
        """Comments are published on the LiveVideo's underlying Video object."""
        extra = self.ctx.credentials.extra
        if extra.get("_video_id_for") == self.live_video_id and extra.get("_video_id"):
            return str(extra["_video_id"])
        data = await self._get(
            f"{self.graph_url}/{self._v}/{self.live_video_id}", {"fields": "video"}
        )
        video_id = str((data.get("video") or {}).get("id") or "")
        if not video_id:
            msg = "facebook: live video has no video object yet"
            raise AdapterError(msg)
        extra["_video_id"] = video_id
        extra["_video_id_for"] = self.live_video_id
        return video_id

    async def send(self, text: str, *, reply_to: str | None = None) -> SendResult:
        """``POST /{video-id}/comments`` with the Page token (``pages_manage_engagement``)."""
        try:
            if not self._token:
                self._token = self._resolve_token()
            if not self.live_video_id:
                self.live_video_id = await self._find_live_video()
            target = reply_to or await self._video_id()
            resp = await self.ctx.http.post(
                f"{self.graph_url}/{self._v}/{target}/comments",
                data={"message": text[:8000], "access_token": self._token},
                timeout=20,
            )
        except AdapterError as exc:
            return SendResult(False, error=str(exc))
        except httpx.HTTPError as exc:
            return SendResult(False, error=f"facebook graph unreachable: {exc}")
        if resp.status_code >= 400:
            return SendResult(False, error=str(_graph_error(resp)))
        return SendResult(True, message_id=str(resp.json().get("id", "")))

    async def close(self) -> None:
        self._stop = True
