from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from core.models import ChatMessage as ChatMessageRow
from core.redis import KEY_OAUTH_STATE, chat_status_key
from core.settings import Settings

ACC = "/api/v1/chat/accounts"


@pytest.fixture
def chat_client(
    settings: Settings, fake_redis: fakeredis.aioredis.FakeRedis, admin_hash: str
) -> Iterator[TestClient]:
    from contextlib import asynccontextmanager

    from api.main import create_app

    app = create_app(
        settings.model_copy(
            update={
                "allow_fake_chat": True,
                "twitch_client_id": "tc",
                "twitch_client_secret": settings.twitch_client_secret.__class__("ts"),
            }
        )
    )
    original = app.router.lifespan_context

    @asynccontextmanager
    async def patched(a):
        async with original(a):
            await a.state.redis.aclose()
            a.state.redis = fake_redis
            yield

    app.router.lifespan_context = patched
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        assert r.status_code == 200
        yield c


def test_accounts_require_auth(client: TestClient) -> None:
    assert client.get(ACC).status_code == 401
    assert client.get("/api/v1/chat/messages").status_code == 401
    assert client.get("/api/v1/chat/oauth/twitch/start").status_code == 401


def test_platforms(chat_client: TestClient) -> None:
    items = {p["platform"]: p for p in chat_client.get("/api/v1/chat/platforms").json()}
    assert items["twitch"]["oauth_configured"] is True
    assert items["youtube"]["oauth_configured"] is False
    assert "fake" in items


def test_manual_account_crud_and_encryption(
    chat_client: TestClient, settings: Settings, redis_sync: fakeredis.FakeRedis
) -> None:
    r = chat_client.post(
        ACC,
        json={
            "platform": "twitch",
            "channel_ref": "streamer25",
            "access_token": "oauth:secret123",
            "refresh_token": "r",
        },
    )
    assert r.status_code == 201, r.text
    acc = r.json()
    assert acc["platform"] == "twitch"
    assert acc["has_credentials"] is True
    assert acc["status"] == "idle"
    assert "access_token" not in json.dumps(acc)
    db_path = settings.database_url.replace("sqlite:///", "")
    raw = (
        sqlite3.connect(db_path)
        .execute("select credentials_encrypted from chat_account")
        .fetchone()[0]
    )
    assert "secret123" not in raw
    assert raw.startswith("gAAAA")

    redis_sync.set(
        chat_status_key(acc["id"]),
        json.dumps(
            {
                "id": acc["id"],
                "platform": "twitch",
                "name": "x",
                "status": "connected",
                "detail": None,
                "last_error": None,
                "updated_at": 1.0,
            }
        ),
    )
    listed = chat_client.get(ACC).json()
    assert listed[0]["status"] == "connected"

    r = chat_client.patch(f"{ACC}/{acc['id']}", json={"enabled": False, "display_name": "Main"})
    assert r.json()["enabled"] is False
    assert r.json()["status"] == "disabled"
    assert r.json()["display_name"] == "Main"
    r = chat_client.patch(f"{ACC}/{acc['id']}", json={"access_token": "newtok"})
    assert r.status_code == 200
    assert chat_client.delete(f"{ACC}/{acc['id']}").status_code == 204
    assert chat_client.get(ACC).json() == []
    assert chat_client.patch(f"{ACC}/99", json={"enabled": True}).status_code == 404


def test_fake_platform_gate(auth_client: TestClient, chat_client: TestClient) -> None:
    assert (
        auth_client.post(ACC, json={"platform": "fake"}).status_code == 422
    )  # allow_fake_chat False
    assert chat_client.post(ACC, json={"platform": "fake"}).status_code == 201


def test_history_pagination_and_filters(chat_client: TestClient) -> None:
    engine = chat_client.app.state.engine  # type: ignore[attr-defined]
    base = datetime(2026, 10, 25, 19, 0, tzinfo=UTC)
    with Session(engine) as session:
        for i in range(6):
            session.add(
                ChatMessageRow(
                    id=f"fake:{i}",
                    platform="fake" if i % 2 else "twitch",
                    author=f"u{i}",
                    author_id=str(i),
                    text=f"m{i}",
                    timestamp=base + timedelta(seconds=i),
                )
            )
        session.commit()
    msgs = chat_client.get("/api/v1/chat/messages", params={"limit": 4}).json()
    assert [m["text"] for m in msgs] == ["m2", "m3", "m4", "m5"]  # newest 4, oldest first
    older = chat_client.get(
        "/api/v1/chat/messages", params={"limit": 4, "before": msgs[0]["timestamp"]}
    ).json()
    assert [m["text"] for m in older] == ["m0", "m1"]
    only = chat_client.get("/api/v1/chat/messages", params={"platform": "twitch"}).json()
    assert {m["platform"] for m in only} == {"twitch"}


def test_oauth_start_and_callback_state_handling(
    chat_client: TestClient, redis_sync: fakeredis.FakeRedis
) -> None:
    r = chat_client.get("/api/v1/chat/oauth/twitch/start")
    assert r.status_code == 200
    url = r.json()["url"]
    assert url.startswith("https://id.twitch.tv/oauth2/authorize?")
    state = url.split("state=")[1].split("&")[0]
    assert redis_sync.get(KEY_OAUTH_STATE.format(state=state))
    assert chat_client.get("/api/v1/chat/oauth/youtube/start").status_code == 409  # not configured
    assert chat_client.get("/api/v1/chat/oauth/myspace/start").status_code == 404

    cb = chat_client.get(
        "/api/v1/chat/oauth/twitch/callback",
        params={"code": "c", "state": "bogus"},
        follow_redirects=False,
    )
    assert cb.status_code == 303
    assert "chat_error" in cb.headers["location"]
    cb = chat_client.get(
        "/api/v1/chat/oauth/twitch/callback",
        params={"error": "access_denied", "error_description": "user said no"},
        follow_redirects=False,
    )
    assert cb.status_code == 303
    assert "user" in cb.headers["location"]


def test_delete_account_keeps_history(chat_client: TestClient) -> None:
    acc = chat_client.post(ACC, json={"platform": "fake", "display_name": "d"}).json()
    engine = chat_client.app.state.engine  # type: ignore[attr-defined]
    with Session(engine) as session:
        session.add(
            ChatMessageRow(
                id="fake:keep",
                platform="fake",
                chat_account_id=acc["id"],
                author="a",
                author_id="1",
                text="t",
                timestamp=datetime.now(UTC),
            )
        )
        session.commit()
    assert chat_client.delete(f"{ACC}/{acc['id']}").status_code == 204
    msgs = chat_client.get("/api/v1/chat/messages").json()
    assert [m["id"] for m in msgs] == ["fake:keep"]
    assert msgs[0]["account_id"] is None
