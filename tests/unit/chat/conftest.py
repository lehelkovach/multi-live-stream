from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import httpx
import pytest

from chat.base import AccountRef, AdapterContext, Credentials
from core.settings import Settings

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "chat"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class SavedState:
    def __init__(self) -> None:
        self.credentials: list[Credentials] = []
        self.identity: list[tuple[str, str, str]] = []

    async def save_credentials(self, creds: Credentials) -> None:
        self.credentials.append(creds)

    async def save_identity(self, external_id: str, display_name: str, channel_ref: str) -> None:
        self.identity.append((external_id, display_name, channel_ref))


@pytest.fixture
def chat_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "twitch_client_id": "twitch-client",
            "twitch_client_secret": settings.twitch_client_secret.__class__("twitch-secret"),
            "google_client_id": "google-client",
            "google_client_secret": settings.google_client_secret.__class__("google-secret"),
            "kick_client_id": "kick-client",
            "kick_client_secret": settings.kick_client_secret.__class__("kick-secret"),
            "facebook_app_id": "fb-app",
            "facebook_app_secret": settings.facebook_app_secret.__class__("fb-secret"),
            "allow_fake_chat": True,
            "domain": "stream.example.com",
            # a budget big enough that quota pacing never slows the adapter tests down
            "youtube_daily_quota": 100_000_000,
        }
    )


@pytest.fixture
def saved() -> SavedState:
    return SavedState()


def make_ctx(
    settings: Settings,
    platform: str,
    handler: Any,
    *,
    saved: SavedState | None = None,
    redis: fakeredis.aioredis.FakeRedis | None = None,
    credentials: Credentials | None = None,
    channel_ref: str = "",
    external_id: str = "",
    display_name: str = "Streamer",
) -> AdapterContext:
    transport = httpx.MockTransport(handler)
    return AdapterContext(
        account=AccountRef(
            id=7,
            platform=platform,
            channel_ref=channel_ref,
            external_id=external_id,
            display_name=display_name,
        ),
        credentials=credentials or Credentials(access_token="tok", refresh_token="ref"),
        settings=settings,
        http=httpx.AsyncClient(transport=transport),
        redis=redis,
        save_credentials=saved.save_credentials if saved else None,
        save_identity=saved.save_identity if saved else None,
    )
