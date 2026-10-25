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

from sqlalchemy import JSON, Column, ForeignKey, Index, Integer
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


class ChatPlatform(StrEnum):
    TWITCH = "twitch"
    YOUTUBE = "youtube"
    KICK = "kick"
    FACEBOOK = "facebook"
    FAKE = "fake"  # test/demo adapter; only creatable when OMNI_ALLOW_FAKE_CHAT=true


class ChatAccountStatus(StrEnum):
    DISABLED = "disabled"
    IDLE = "idle"  # enabled, manager has not picked it up yet
    CONNECTING = "connecting"
    CONNECTED = "connected"  # receiving messages
    WAITING = "waiting"  # connected credentials, but no live broadcast/chat right now
    RETRYING = "retrying"  # transient failure, backing off
    AUTH_ERROR = "auth_error"  # token invalid/revoked; needs reconnecting by the owner
    ERROR = "error"


class ChatAccount(SQLModel, table=True):
    __tablename__ = "chat_account"

    id: int | None = Field(default=None, primary_key=True)
    platform: str = Field(index=True, max_length=32)
    display_name: str = Field(default="", max_length=100)
    # Platform-specific target: Twitch login, YouTube channel id, Kick slug, Facebook page id.
    channel_ref: str = Field(default="", max_length=500)
    # Stable platform identity of the connected user/page (dedupes OAuth re-connects).
    external_id: str = Field(default="", max_length=200, index=True)
    credentials_encrypted: str = Field(default="")
    enabled: bool = Field(default=True)
    status: str = Field(default=ChatAccountStatus.IDLE, max_length=32)
    last_error: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_message"
    __table_args__ = (Index("ix_chat_message_platform_ts", "platform", "timestamp"),)

    id: str = Field(primary_key=True, max_length=200)  # "<platform>:<platform message id>"
    platform: str = Field(max_length=32)
    destination_id: int | None = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("destination.id", ondelete="SET NULL"), nullable=True),
    )
    chat_account_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer, ForeignKey("chat_account.id", ondelete="SET NULL"), nullable=True, index=True
        ),
    )
    author: str = Field(default="", max_length=200)
    author_id: str = Field(default="", max_length=200)
    text: str = Field(default="")
    timestamp: datetime = Field(default_factory=utcnow, index=True)
    is_own: bool = Field(default=False)
    kind: str = Field(default="text", max_length=32)  # text | superchat | system
    badges: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    raw: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))


class OutgoingStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"  # every target accepted it
    PARTIAL = "partial"  # some targets failed
    FAILED = "failed"  # no target accepted it


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    OFFLINE = "offline"  # account not connected right now
    UNSUPPORTED = "unsupported"  # platform cannot post


class OutgoingMessage(SQLModel, table=True):
    __tablename__ = "outgoing_message"

    id: int | None = Field(default=None, primary_key=True)
    text: str
    # ["all"] or chat account ids as strings
    targets: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    # {"<account id>": {"status", "error", "message_id", "platform", "name", "at"}}
    per_target_status: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON, nullable=False)
    )
    status: str = Field(default=OutgoingStatus.PENDING, max_length=16)
    reply_to: str | None = Field(default=None, max_length=200)  # chat_message id being answered
    created_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None
