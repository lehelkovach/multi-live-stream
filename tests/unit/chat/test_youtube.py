from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from chat import oauth
from chat.adapters import youtube as yt
from chat.adapters.youtube import QuotaBudget, YouTubeAdapter, normalize_item
from chat.base import AuthError, NotLiveError
from core.settings import Settings
from tests.unit.chat.conftest import load_fixture, make_ctx

LIST = load_fixture("youtube_list.json")
BROADCASTS = {
    "items": [
        {
            "id": "b1",
            "snippet": {"liveChatId": "Cg0KC2FiYzEyMw", "title": "Live!"},
            "status": {"lifeCycleStatus": "live"},
        },
    ]
}


def test_normalize_items() -> None:
    items = LIST["items"]
    first = normalize_item(items[0], 7, own_channel_id="UCme")
    assert first is not None
    assert first.id == "LCC.msg1"
    assert first.author == "Viewer One"
    assert first.text == "first!"
    assert first.badges == ["member"]
    assert first.kind == "text"
    assert first.timestamp.isoformat().startswith("2026-10-25T19:21:05")
    superchat = normalize_item(items[1], 7, own_channel_id="UCme")
    assert superchat is not None
    assert superchat.kind == "superchat"
    assert superchat.text == "[€5.00] great stream"
    assert superchat.badges == ["moderator"]
    assert normalize_item(items[2], 7, "UCme") is None  # tombstone
    own = normalize_item(
        {**items[0], "authorDetails": {**items[0]["authorDetails"], "channelId": "UCme"}}, 7, "UCme"
    )
    assert own is not None
    assert own.is_own


def make_handler(
    pages: list[dict[str, Any]],
    *,
    broadcasts: dict[str, Any] | None = BROADCASTS,
    log: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if log is not None:
            log.append(f"{request.method} {path}?{request.url.query.decode()}")
        if path.endswith("/liveBroadcasts"):
            assert dict(request.url.params)["broadcastStatus"] == "active"
            return httpx.Response(200, json=broadcasts or {"items": []})
        if path.endswith("/liveChat/messages") and request.method == "GET":
            assert dict(request.url.params)["liveChatId"] == "Cg0KC2FiYzEyMw"
            page = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            if isinstance(page.get("_status"), int):
                return httpx.Response(page["_status"], json=page.get("_body", {}))
            return httpx.Response(200, json=page)
        if path.endswith("/liveChat/messages") and request.method == "POST":
            body = json.loads(request.content)
            assert body["snippet"]["type"] == "textMessageEvent"
            return httpx.Response(200, json={"id": "LCC.sent", "snippet": body["snippet"]})
        if str(request.url).startswith(oauth.GOOGLE_TOKEN):
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}})

    return handler


async def collect(adapter: YouTubeAdapter, n: int, within: float = 5.0) -> list[Any]:
    out: list[Any] = []

    async def run() -> None:
        async for m in adapter.messages():
            out.append(m)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout=within)
    return out


async def test_finds_chat_and_polls_with_page_tokens(
    chat_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(yt, "MIN_INTERVAL", 0.01)
    log: list[str] = []
    page2 = {
        **LIST,
        "items": [{**LIST["items"][0], "id": "LCC.msg4"}],
        "nextPageToken": "T2",
        "pollingIntervalMillis": 10,
    }
    ctx = make_ctx(
        chat_settings,
        "youtube",
        make_handler([{**LIST, "pollingIntervalMillis": 10}, page2], log=log),
        external_id="UCme",
    )
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    assert adapter.live_chat_id == "Cg0KC2FiYzEyMw"
    msgs = await collect(adapter, 3)
    assert [m.id for m in msgs] == ["LCC.msg1", "LCC.msg2", "LCC.msg4"]
    assert any("pageToken=GLiC9ZzGv4kDIJiL1ozGv4kD" in line for line in log)
    await adapter.close()
    assert await adapter.quota.spent() >= yt.COST_BROADCASTS + 2 * yt.COST_LIST


async def test_no_active_broadcast_is_not_live(chat_settings: Settings) -> None:
    ctx = make_ctx(chat_settings, "youtube", make_handler([LIST], broadcasts={"items": []}))
    with pytest.raises(NotLiveError, match="no active"):
        await YouTubeAdapter(ctx).connect()


async def test_live_chat_override_skips_lookup(chat_settings: Settings) -> None:
    from chat.base import Credentials

    creds = Credentials(access_token="tok", extra={"live_chat_id": "Cg0KC2FiYzEyMw"})
    ctx = make_ctx(
        chat_settings, "youtube", make_handler([LIST], broadcasts={"items": []}), credentials=creds
    )
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    assert adapter.live_chat_id == "Cg0KC2FiYzEyMw"


async def test_offline_and_quota_errors(
    chat_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(yt, "MIN_INTERVAL", 0.01)
    ended = {**LIST, "offlineAt": "2026-10-25T20:00:00Z", "pollingIntervalMillis": 10}
    ctx = make_ctx(chat_settings, "youtube", make_handler([ended]))
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    with pytest.raises(NotLiveError, match="ended"):
        await collect(adapter, 10)

    quota = {"_status": 403, "_body": {"error": {"errors": [{"reason": "quotaExceeded"}]}}}
    ctx = make_ctx(chat_settings, "youtube", make_handler([quota]))
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    with pytest.raises(NotLiveError, match="quota"):
        await collect(adapter, 1)

    forbidden = {
        "_status": 403,
        "_body": {"error": {"errors": [{"reason": "insufficientPermissions"}]}},
    }
    ctx = make_ctx(chat_settings, "youtube", make_handler([forbidden]))
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    with pytest.raises(AuthError):
        await collect(adapter, 1)


async def test_401_refreshes_and_retries(
    chat_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(yt, "MIN_INTERVAL", 0.01)
    from tests.unit.chat.conftest import SavedState

    saved = SavedState()
    pages = [
        {"_status": 401, "_body": {"error": {"code": 401}}},
        {**LIST, "pollingIntervalMillis": 10},
    ]
    ctx = make_ctx(chat_settings, "youtube", make_handler(pages), saved=saved)
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    msgs = await collect(adapter, 1)
    assert msgs[0].id == "LCC.msg1"
    assert saved.credentials
    assert saved.credentials[0].access_token == "fresh"


async def test_send(chat_settings: Settings) -> None:
    ctx = make_ctx(chat_settings, "youtube", make_handler([LIST]))
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    res = await adapter.send("hi youtube")
    assert res.ok
    assert res.message_id == "LCC.sent"


async def test_quota_budget_interval(chat_settings: Settings) -> None:
    """Default project quota (10k units, 80% for chat, 3 h/day): pacing is clock-dependent."""
    settings = chat_settings.model_copy(
        update={
            "youtube_daily_quota": 10_000,
            "youtube_chat_quota_share": 0.8,
            "youtube_chat_hours": 3.0,
        }
    )
    ctx = make_ctx(settings, "youtube", make_handler([LIST]))
    afternoon = QuotaBudget(ctx, clock=lambda: datetime(2026, 10, 28, 14, 0, tzinfo=UTC))
    # 8000 units = 1600 list calls to cover 3 h -> one call every 6.75 s
    assert await afternoon.min_interval() == pytest.approx(3 * 3600 / 1600)
    late = QuotaBudget(ctx, clock=lambda: datetime(2026, 10, 28, 23, 30, tzinfo=UTC))
    assert await late.min_interval() == yt.MIN_INTERVAL  # 30 min left: poll at full speed
    await afternoon.spend(4000)  # half the budget gone -> twice the gap
    assert await afternoon.min_interval() == pytest.approx(3 * 3600 / 800)
    await afternoon.spend(4000)  # exhausted -> effectively paused until the reset
    assert await afternoon.min_interval() >= yt.MAX_INTERVAL


async def test_stream_mode_parses_streamed_documents(chat_settings: Settings) -> None:
    docs = [
        json.dumps({**LIST, "items": [LIST["items"][0]]}),
        json.dumps({**LIST, "items": [{**LIST["items"][0], "id": "LCC.msg9"}]}),
    ]
    body = "[" + ",\n".join(docs) + "]"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/liveBroadcasts"):
            return httpx.Response(200, json=BROADCASTS)
        if request.url.path.endswith("/stream"):
            return httpx.Response(200, content=body.encode())
        return httpx.Response(404)

    ctx = make_ctx(
        chat_settings.model_copy(update={"youtube_stream_mode": True}), "youtube", handler
    )
    adapter = YouTubeAdapter(ctx)
    await adapter.connect()
    got = []
    async for m in adapter.messages():
        got.append(m.id)
        if len(got) == 2:
            break
    assert got == ["LCC.msg1", "LCC.msg9"]
