"""Chat adapter contract shared by every platform module.

An adapter wraps one connected account. The manager (``chat/manager.py``) owns the
lifecycle: ``connect()`` -> iterate ``messages()`` -> ``close()``, with retries. An
adapter signals *why* it cannot deliver messages with the exception types below so
the manager can pick the right wait: ``NotLiveError`` (poll again later),
``AuthError`` (owner must reconnect; retry slowly), anything else (back off).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx
import redis.asyncio as aioredis

from core.settings import Settings


@dataclass(slots=True)
class ChatMessage:
    """Normalized chat message (platform ids are prefixed when persisted)."""

    id: str  # platform's own message id
    platform: str
    author: str
    author_id: str
    text: str
    timestamp: datetime
    is_own: bool = False
    kind: str = "text"  # text | superchat | system
    badges: list[str] = field(default_factory=list)  # moderator, subscriber, owner, ...
    raw: dict[str, Any] | None = None
    account_id: int | None = None

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    def to_event(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "platform": self.platform,
            "account_id": self.account_id,
            "author": self.author,
            "author_id": self.author_id,
            "text": self.text,
            "timestamp": self.timestamp.astimezone(UTC).isoformat(),
            "is_own": self.is_own,
            "kind": self.kind,
            "badges": list(self.badges),
        }


@dataclass(slots=True)
class SendResult:
    ok: bool
    message_id: str | None = None
    error: str | None = None


class AdapterError(Exception):
    """Transient failure; the manager retries with backoff."""


class AuthError(AdapterError):
    """Credentials are invalid or revoked; the owner has to reconnect the account."""


class NotLiveError(AdapterError):
    """Credentials work but there is no live broadcast / chat to attach to right now."""


class ConfigError(AdapterError):
    """Missing settings (OAuth app credentials, channel reference...)."""


@dataclass
class Credentials:
    """Stored encrypted (JSON) in ``ChatAccount.credentials_encrypted``."""

    access_token: str = ""
    refresh_token: str = ""
    expires_at: float | None = None  # unix timestamp
    token_type: str = "bearer"
    scopes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # page tokens, overrides, ...

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> Credentials:
        if not raw:
            return cls()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return cls()
        return cls(
            access_token=str(data.get("access_token", "")),
            refresh_token=str(data.get("refresh_token", "")),
            expires_at=data.get("expires_at"),
            token_type=str(data.get("token_type", "bearer")),
            scopes=[str(s) for s in data.get("scopes", [])],
            extra=dict(data.get("extra") or {}),
        )

    def expires_within(self, seconds: float) -> bool:
        return self.expires_at is not None and self.expires_at - time.time() < seconds


@dataclass(slots=True)
class AccountRef:
    """The bits of a ``ChatAccount`` row an adapter needs (no SQL session attached)."""

    id: int
    platform: str
    channel_ref: str
    external_id: str
    display_name: str


SaveCredentials = Callable[[Credentials], Awaitable[None]]
SaveIdentity = Callable[[str, str, str], Awaitable[None]]  # external_id, display_name, channel_ref


@dataclass
class AdapterContext:
    account: AccountRef
    credentials: Credentials
    settings: Settings
    http: httpx.AsyncClient
    redis: aioredis.Redis | None = None
    save_credentials: SaveCredentials | None = None
    save_identity: SaveIdentity | None = None

    async def persist_credentials(self) -> None:
        if self.save_credentials is not None:
            await self.save_credentials(self.credentials)

    async def persist_identity(self, external_id: str, display_name: str, channel_ref: str) -> None:
        self.account.external_id = external_id
        self.account.display_name = display_name or self.account.display_name
        self.account.channel_ref = channel_ref or self.account.channel_ref
        if self.save_identity is not None:
            await self.save_identity(
                external_id, self.account.display_name, self.account.channel_ref
            )


@runtime_checkable
class ChatAdapter(Protocol):
    platform: str

    async def connect(self) -> None: ...

    def messages(self) -> AsyncIterator[ChatMessage]: ...

    async def send(self, text: str, *, reply_to: str | None = None) -> SendResult: ...

    async def close(self) -> None: ...


def parse_timestamp(value: str | None) -> datetime:
    """ISO-8601 (with Z or offset) -> aware datetime; falls back to now."""
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return datetime.now(UTC)
