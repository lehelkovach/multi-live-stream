"""Core data model (SQLModel tables + value enums).

Enum-like columns are stored as plain strings (portable between SQLite and
Postgres); the allowed values are validated at the API boundary.
Credentials are stored encrypted (see ``core.crypto``); column names end in
``_encrypted`` so a plaintext never lands in the DB by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Column, Index
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Platform(StrEnum):
    YOUTUBE = "youtube"
    FACEBOOK = "facebook"
    TWITCH = "twitch"
    KICK = "kick"
    X = "x"
    CUSTOM = "custom"
    # Phase 5: mobile-app platforms (e.g. "meetme") are added here.


class Profile(StrEnum):
    COPY = "copy"  # -c copy, never re-encode (default)
    VERTICAL_720P = "vertical_720p"  # crop centre to 9:16 and scale to 720x1280
    CUSTOM = "custom"  # operator-supplied ffmpeg output args (see Destination.ffmpeg_args)


class DestinationStatus(StrEnum):
    DISABLED = "disabled"  # enabled=False
    IDLE = "idle"  # enabled, but no input stream is live
    STARTING = "starting"  # ffmpeg launched, waiting for first output bytes
    LIVE = "live"  # ffmpeg running and pushing
    RETRYING = "retrying"  # ffmpeg exited; waiting out the backoff
    ERROR = "error"  # gave up (max backoff) or permanent failure


class Destination(SQLModel, table=True):
    __tablename__ = "destination"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, max_length=100)
    platform: str = Field(default=Platform.CUSTOM, max_length=32)
    rtmp_url: str = Field(max_length=500)
    stream_key_encrypted: str = Field(default="")
    enabled: bool = Field(default=True, index=True)
    profile: str = Field(default=Profile.COPY, max_length=32)
    # Only used when profile == "custom": ffmpeg output options placed between the
    # input and the output URL (e.g. "-c:v libx264 -b:v 3000k -c:a copy").
    ffmpeg_args: str | None = Field(default=None, max_length=2000)
    status: str = Field(default=DestinationStatus.IDLE, max_length=32)
    last_error: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def output_url(self, stream_key: str) -> str:
        """``rtmp_url`` + ``/`` + key, tolerating a trailing slash on the server URL."""
        base = self.rtmp_url.rstrip("/")
        return f"{base}/{stream_key}" if stream_key else base


class AppSetting(SQLModel, table=True):
    """Tiny key/value table for global switches (e.g. ``relay.enabled``)."""

    __tablename__ = "app_setting"

    key: str = Field(primary_key=True, max_length=100)
    value: str = Field(default="", max_length=2000)
    updated_at: datetime = Field(default_factory=utcnow)


SETTING_RELAY_ENABLED = "relay.enabled"


class ChatAccount(SQLModel, table=True):
    __tablename__ = "chat_account"

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(index=True, max_length=32)
    display_name: str = Field(default="", max_length=100)
    channel_ref: str = Field(default="", max_length=500)
    credentials_encrypted: str = Field(default="")
    enabled: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_message"
    __table_args__ = (Index("ix_chat_message_platform_ts", "platform", "timestamp"),)

    id: str = Field(primary_key=True, max_length=200)  # "<platform>:<platform message id>"
    platform: str = Field(max_length=32)
    destination_id: int | None = Field(default=None, foreign_key="destination.id")
    chat_account_id: int | None = Field(default=None, foreign_key="chat_account.id")
    author: str = Field(default="", max_length=200)
    author_id: str = Field(default="", max_length=200)
    text: str = Field(default="")
    timestamp: datetime = Field(default_factory=utcnow, index=True)
    is_own: bool = Field(default=False)
    raw: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))


class OutgoingMessage(SQLModel, table=True):
    __tablename__ = "outgoing_message"

    id: int | None = Field(default=None, primary_key=True)
    text: str
    targets: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    per_target_status: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    created_at: datetime = Field(default_factory=utcnow, index=True)
