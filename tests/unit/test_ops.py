from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from api import cli
from core.models import ChatMessage, OutgoingMessage
from core.redis import KEY_RELAY_HEARTBEAT, dest_status_key
from core.settings import Settings


def test_metrics_requires_session(client: TestClient) -> None:
    assert client.get("/metrics").status_code == 401
    assert client.get("/api/v1/stats").status_code == 401


def test_metrics_with_session(auth_client: TestClient) -> None:
    r = auth_client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "omnistream_ingest_live 0.0" in body
    assert 'omnistream_service_heartbeat_age_seconds{service="relay"} +Inf' in body


def test_metrics_bearer_token(settings: Settings, fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    from contextlib import asynccontextmanager

    from api.main import create_app

    app = create_app(
        settings.model_copy(update={"metrics_token": settings.metrics_token.__class__("scrape-me")})
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
        assert c.get("/metrics").status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/metrics", headers={"Authorization": "Bearer scrape-me"}).status_code == 200


def test_metrics_and_stats_reflect_state(
    auth_client: TestClient, redis_sync: fakeredis.FakeRedis
) -> None:
    dest = auth_client.post(
        "/api/v1/destinations",
        json={
            "name": "YT main",
            "platform": "youtube",
            "rtmp_url": "rtmp://a/b",
            "stream_key": "k",
        },
    ).json()
    redis_sync.set(KEY_RELAY_HEARTBEAT, str(time.time()))
    redis_sync.set(
        dest_status_key(dest["id"]),
        json.dumps(
            {
                "id": dest["id"],
                "status": "live",
                "bitrate_kbps": 2500.5,
                "uptime_s": 42,
                "restarts": 1,
            }
        ),
    )
    redis_sync.set(
        "omni:ingest:state", json.dumps({"live": True, "bitrate_kbps": 6000.0, "tracks": ["H264"]})
    )
    engine = auth_client.app.state.engine  # type: ignore[attr-defined]
    with Session(engine) as session:
        session.add(
            ChatMessage(
                id="twitch:1",
                platform="twitch",
                author="a",
                author_id="1",
                text="x",
                timestamp=datetime.now(UTC),
            )
        )
        session.add(
            ChatMessage(
                id="twitch:2",
                platform="twitch",
                author="a",
                author_id="1",
                text="y",
                timestamp=datetime.now(UTC),
            )
        )
        session.add(
            ChatMessage(
                id="twitch:old",
                platform="twitch",
                author="a",
                author_id="1",
                text="z",
                timestamp=datetime.now(UTC) - timedelta(days=2),
            )
        )
        session.add(
            OutgoingMessage(text="hi", targets=["all"], per_target_status={}, status="sent")
        )
        session.commit()
    body = auth_client.get("/metrics").text
    assert "omnistream_ingest_live 1.0" in body
    assert (
        'omnistream_destination_status{destination="YT main",platform="youtube",status="live"} 1.0'
        in body
    )
    assert (
        'omnistream_destination_bitrate_kbps{destination="YT main",platform="youtube"} 2500.5'
        in body
    )
    assert 'omnistream_chat_messages_today{platform="twitch"} 2.0' in body
    assert 'omnistream_chat_outgoing_today{status="sent"} 1.0' in body
    stats = auth_client.get("/api/v1/stats").json()
    assert stats["ingest"]["live"] is True
    assert stats["destinations"][0]["status"] == "live"
    assert stats["messages_today"] == {"twitch": 2}
    assert stats["relay_heartbeat_age_s"] < 5


def test_cli_backup_and_prune(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from core.db import init_db, make_engine
    from core.settings import get_settings

    db_path = settings.database_url.removeprefix("sqlite:///")
    engine = make_engine(settings.database_url)
    init_db(engine)
    with Session(engine) as session:
        session.add(
            ChatMessage(
                id="fake:new",
                platform="fake",
                author="a",
                author_id="1",
                text="keep",
                timestamp=datetime.now(UTC),
            )
        )
        session.add(
            ChatMessage(
                id="fake:old",
                platform="fake",
                author="a",
                author_id="1",
                text="drop",
                timestamp=datetime.now(UTC) - timedelta(days=40),
            )
        )
        session.add(
            OutgoingMessage(
                text="old",
                targets=["all"],
                per_target_status={},
                created_at=datetime.now(UTC) - timedelta(days=40),
            )
        )
        session.commit()
    monkeypatch.setenv("OMNI_SECRET_KEY", settings.secret_key.get_secret_value())
    monkeypatch.setenv("OMNI_SESSION_SECRET", settings.session_secret.get_secret_value())
    monkeypatch.setenv("OMNI_DATABASE_URL", settings.database_url)
    get_settings.cache_clear()

    backups = tmp_path / "bk"
    for _ in range(3):
        assert cli.main(["backup", "--dir", str(backups), "--keep", "2"]) == 0
        time.sleep(1.1)  # distinct timestamps
    files = sorted(backups.glob("omnistream-*.db"))
    assert len(files) == 2
    copy = sqlite3.connect(files[-1])
    assert copy.execute("select count(*) from chat_message").fetchone()[0] == 2
    assert "back up .env" in capsys.readouterr().out
    assert Path(db_path).exists()

    assert cli.main(["prune", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "pruned 1 chat messages and 1 outgoing messages" in out
    with Session(engine) as session:
        remaining = [m.id for m in session.exec(__import__("sqlmodel").select(ChatMessage)).all()]
    assert remaining == ["fake:new"]
    get_settings.cache_clear()
