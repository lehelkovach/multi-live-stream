from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from chat.adapters import facebook as fb
from chat.adapters.facebook import FacebookAdapter, normalize_comment
from chat.base import AuthError, ConfigError, Credentials, NotLiveError
from core.settings import Settings
from tests.unit.chat.conftest import load_fixture, make_ctx

COMMENTS = load_fixture("facebook_comments.json")
LIVE_VIDEOS = {"data": [{"id": "9876543210", "status": "LIVE", "title": "Tonight"}]}


def test_normalize_comment() -> None:
    msg = normalize_comment(COMMENTS["data"][0], 7, page_id="555000111")
    assert msg.id == "9876543210_2"
    assert msg.author == "Pat Example"
    assert msg.author_id == "10203040506070"
    assert msg.text == "watching from Berlin"
    assert msg.is_own is False
    assert msg.timestamp.isoformat().startswith("2026-10-25T19:23:30")
    own = normalize_comment(COMMENTS["data"][1], 7, page_id="555000111")
    assert own.is_own is True


def make_handler(
    *,
    sse_status: int = 200,
    sse_lines: list[str] | None = None,
    comments: list[dict[str, Any]] | None = None,
    live: dict[str, Any] | None = LIVE_VIDEOS,
    log: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if log is not None:
            log.append(f"{url.host}{url.path}")
        assert dict(url.params).get("access_token") == "pagetok", url
        if url.host == "streaming-graph.facebook.com":
            if sse_status != 200:
                return httpx.Response(
                    sse_status, json={"error": {"message": "not allowed", "code": 100}}
                )
            return httpx.Response(200, content="\n".join(sse_lines or []).encode())
        if url.path.endswith("/live_videos"):
            return httpx.Response(200, json=live or {"data": []})
        if url.path.endswith("/comments"):
            polls["n"] += 1
            batch = comments[min(polls["n"] - 1, len(comments) - 1)] if comments else COMMENTS
            return httpx.Response(200, json=batch)
        if url.path.endswith("/9876543210"):
            return httpx.Response(200, json={"id": "9876543210", "status": "LIVE"})
        return httpx.Response(404, json={"error": {"message": "unknown", "code": 803}})

    return handler


CREDS = Credentials(
    access_token="usertok",
    extra={"pages": [{"id": "555000111", "name": "Streamer Page", "access_token": "pagetok"}]},
)


async def collect(adapter: FacebookAdapter, n: int, within: float = 5.0) -> list[Any]:
    out: list[Any] = []

    async def run() -> None:
        async for m in adapter.messages():
            out.append(m)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout=within)
    return out


async def test_sse_stream(chat_settings: Settings) -> None:
    lines = [
        ": ping",
        "",
        "data: "
        + json.dumps(
            {
                "id": "c1",
                "from": {"name": "A", "id": "1"},
                "message": "hi",
                "created_time": "2026-10-25T19:24:00+0000",
            }
        ),
        "",
        "data: "
        + json.dumps(
            {
                "id": "c2",
                "from": {"name": "B", "id": "2"},
                "message": "yo",
                "created_time": "2026-10-25T19:24:01+0000",
            }
        ),
    ]
    ctx = make_ctx(
        chat_settings,
        "facebook",
        make_handler(sse_lines=lines),
        credentials=CREDS,
        channel_ref="555000111",
    )
    adapter = FacebookAdapter(ctx)
    await adapter.connect()
    assert adapter.live_video_id == "9876543210"
    msgs = await collect(adapter, 2)
    assert [m.id for m in msgs] == ["c1", "c2"]
    await adapter.close()


async def test_sse_fallback_to_polling_dedupes(
    chat_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb, "POLL_INTERVAL", 0.01)
    log: list[str] = []
    newer = {
        "data": [
            {
                "created_time": "2026-10-25T19:23:50+0000",
                "from": {"name": "New", "id": "3"},
                "message": "late",
                "id": "9876543210_3",
            },
            *COMMENTS["data"],
        ]
    }
    ctx = make_ctx(
        chat_settings,
        "facebook",
        make_handler(sse_status=400, comments=[COMMENTS, COMMENTS, newer], log=log),
        credentials=CREDS,
        channel_ref="555000111",
    )
    adapter = FacebookAdapter(ctx)
    await adapter.connect()
    msgs = await collect(adapter, 3)
    assert [m.id for m in msgs] == [
        "9876543210_1",
        "9876543210_2",
        "9876543210_3",
    ]  # chronological, no dupes
    assert any("streaming-graph" in line for line in log)
    await adapter.close()


async def test_page_selection_and_not_live(chat_settings: Settings) -> None:
    two_pages = Credentials(
        access_token="u",
        extra={
            "pages": [
                {"id": "1", "name": "A", "access_token": "pagetok"},
                {"id": "2", "name": "B", "access_token": "pagetok"},
            ]
        },
    )
    ctx = make_ctx(chat_settings, "facebook", make_handler(), credentials=two_pages)
    with pytest.raises(ConfigError, match="choose"):
        await FacebookAdapter(ctx).connect()
    ctx = make_ctx(
        chat_settings,
        "facebook",
        make_handler(live={"data": []}),
        credentials=CREDS,
        channel_ref="555000111",
    )
    with pytest.raises(NotLiveError, match="no live"):
        await FacebookAdapter(ctx).connect()


async def test_graph_auth_error(chat_settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "Error validating access token", "code": 190}}
        )

    ctx = make_ctx(chat_settings, "facebook", handler, credentials=CREDS, channel_ref="555000111")
    with pytest.raises(AuthError, match="validating"):
        await FacebookAdapter(ctx).connect()
